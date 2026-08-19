"""
Task 6: Admin Dashboard analytics.

Every function here does its aggregation in the database (Count, Sum, F,
Q, Case/When, Coalesce, TruncDate, TruncMonth, ExtractHour) and returns
only small, already-aggregated result sets. Nothing in this module loads
raw Booking/Payment rows into Python and loops over them.

Revenue is always computed directly against Payment, never through
Booking, to avoid double-counting a multi-seat payment. Occupancy and
"top theaters" deliberately run Booking-side and Seat/Payment-side
aggregates as SEPARATE queries, merged by id in Python afterward, rather
than one combined multi-relation annotate() that would fan out and
multiply rows.
"""
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db.models import Count, Sum, F, Q
from django.db.models.functions import Coalesce, TruncDate, TruncMonth, ExtractHour
from django.contrib.auth.models import User
from django.utils import timezone

from .models import Booking, Payment, Seat, Theater, Movie, PaymentStatus, RefundStatus

# Statuses representing money that was genuinely captured by Razorpay at
# some point, regardless of what happened afterward (including a later
# refund). This is the single source of truth for "gross revenue" used
# everywhere in this module - never redefined per-function.
CAPTURED_STATUSES = [
    PaymentStatus.SUCCESS,
    PaymentStatus.SUCCESS_UNFULFILLED,
    PaymentStatus.REFUNDED,
    PaymentStatus.PARTIALLY_REFUNDED,
]


def resolve_date_range(start_date, end_date):
    """
    Converts start_date/end_date (date objects, already parsed and
    validated by the caller: start_date <= end_date) into a half-open,
    timezone-aware [start_dt, end_exclusive) interval using Django's
    currently configured timezone. This is what makes "include the
    entire end date" correct without relying on a fragile 23:59:59.999999
    boundary, and avoids naive/aware datetime comparison errors against
    fields stored with USE_TZ=True.
    """
    start_dt = timezone.make_aware(datetime.combine(start_date, time.min))
    end_exclusive = timezone.make_aware(datetime.combine(end_date + timedelta(days=1), time.min))
    return start_dt, end_exclusive


def default_date_range(days=30):
    """Default dashboard window when no explicit range is requested."""
    today = timezone.localdate()
    start_date = today - timedelta(days=days - 1)
    return start_date, today


# ---------------------------------------------------------------------
# Revenue
# ---------------------------------------------------------------------

def _revenue_totals(start_dt, end_exclusive):
    """
    Gross and net revenue for one date range. Runs as ONE query directly
    against Payment - no Booking join, so a multi-seat payment (multiple
    Booking rows, one Payment row) is counted exactly once.
    """
    result = Payment.objects.filter(
        status__in=CAPTURED_STATUSES,
        created_at__gte=start_dt,
        created_at__lt=end_exclusive,
    ).aggregate(
        gross=Coalesce(Sum('amount_rupees'), Decimal('0')),
        net=Coalesce(Sum(F('amount_rupees') - F('refunded_amount')), Decimal('0')),
    )
    return result['gross'], result['net']


def revenue_summary_cards():
    """
    Fixed-calendar-period headline cards (today/week/month/year gross
    revenue) - these represent their actual current calendar periods and
    are NOT affected by the dashboard's custom date filter.
    """
    now_local = timezone.localtime()
    today = now_local.date()

    today_start, today_end = resolve_date_range(today, today)

    week_start_date = today - timedelta(days=today.weekday())
    week_start, week_end = resolve_date_range(week_start_date, today)

    month_start_date = today.replace(day=1)
    month_start, month_end = resolve_date_range(month_start_date, today)

    year_start_date = today.replace(month=1, day=1)
    year_start, year_end = resolve_date_range(year_start_date, today)

    today_gross, _ = _revenue_totals(today_start, today_end)
    week_gross, _ = _revenue_totals(week_start, week_end)
    month_gross, _ = _revenue_totals(month_start, month_end)
    year_gross, _ = _revenue_totals(year_start, year_end)

    return {
        'today_gross': today_gross,
        'week_gross': week_gross,
        'month_gross': month_gross,
        'year_gross': year_gross,
    }


def selected_range_summary(start_dt, end_exclusive):
    """
    Summary numbers for the user-selected custom date range: gross/net
    revenue, confirmed booking count, cancellations, total refunded amount.
    """
    gross, net = _revenue_totals(start_dt, end_exclusive)

    confirmed_bookings = Booking.objects.filter(
        booked_at__gte=start_dt, booked_at__lt=end_exclusive
    ).count()

    cancellations = Payment.objects.filter(
        status=PaymentStatus.CANCELLED,
        created_at__gte=start_dt, created_at__lt=end_exclusive,
    ).count()

    total_refunded = Payment.objects.filter(
        created_at__gte=start_dt, created_at__lt=end_exclusive,
    ).aggregate(total=Coalesce(Sum('refunded_amount'), Decimal('0')))['total']

    return {
        'gross_revenue': gross,
        'net_revenue': net,
        'confirmed_bookings': confirmed_bookings,
        'cancellations': cancellations,
        'total_refunded': total_refunded,
    }


def revenue_trend(start_dt, end_exclusive):
    """
    Daily gross revenue within the selected range, for a trend chart/table.
    Single grouped query - values('day').annotate(...) returns one row per
    day with data, not one row per Payment.
    """
    return list(
        Payment.objects.filter(
            status__in=CAPTURED_STATUSES,
            created_at__gte=start_dt, created_at__lt=end_exclusive,
        )
        .annotate(day=TruncDate('created_at'))
        .values('day')
        .annotate(gross=Coalesce(Sum('amount_rupees'), Decimal('0')))
        .order_by('day')
    )


# ---------------------------------------------------------------------
# Booking trend
# ---------------------------------------------------------------------

def booking_trend(start_dt, end_exclusive):
    """Daily confirmed booking counts within the selected range."""
    return list(
        Booking.objects.filter(
            booked_at__gte=start_dt, booked_at__lt=end_exclusive,
        )
        .annotate(day=TruncDate('booked_at'))
        .values('day')
        .annotate(count=Count('id'))
        .order_by('day')
    )


# ---------------------------------------------------------------------
# Occupancy - confirmed Booking count / total Seat count, per theater/show
# ---------------------------------------------------------------------

def occupancy_by_theater(start_dt, end_exclusive):
    """
    occupancy = confirmed Booking count for a Theater/show whose SHOW TIME
                (Theater.time) falls inside the selected range
                / total Seat count belonging to that Theater/show * 100

    IMPORTANT: this filters by Theater.time (when the SHOW happens), not
    by Booking.booked_at (when the booking was MADE) - a booking made
    weeks before the selected range still counts toward occupancy if its
    show falls inside the range. This is deliberately different from
    booking_trend()/peak_booking_hours(), which filter Booking.booked_at
    because those metrics ask when bookings were made, not when shows occur.

    Three separate queries, merged by theater_id in Python:
      1. Which theater/show IDs fall inside the range (Theater.time)
      2. ALL confirmed Booking counts for exactly those theater IDs (no
         booked_at filter at all)
      3. Seat totals for exactly those same theater IDs
    Never one combined Theater.annotate() with both a Booking Count and a
    Seat Count in the same call, which would fan out and multiply both.
    """
    theater_ids = list(
        Theater.objects.filter(
            time__gte=start_dt, time__lt=end_exclusive,
        ).values_list('id', flat=True)
    )

    booking_counts = {
        row['theater_id']: row['confirmed']
        for row in (
            Booking.objects.filter(theater_id__in=theater_ids)
            .values('theater_id')
            .annotate(confirmed=Count('id'))
        )
    }

    seat_totals = {
        row['theater_id']: row['total']
        for row in (
            Seat.objects.filter(theater_id__in=theater_ids)
            .values('theater_id')
            .annotate(total=Count('id'))
        )
    }

    theaters = Theater.objects.filter(id__in=theater_ids).select_related('movie').order_by('-time')

    results = []
    for theater in theaters:
        confirmed = booking_counts.get(theater.id, 0)
        total_seats = seat_totals.get(theater.id, 0)
        occupancy_percent = round((confirmed / total_seats) * 100, 1) if total_seats else 0
        results.append({
            'theater': theater,
            'confirmed_bookings': confirmed,
            'total_seats': total_seats,
            'occupancy_percent': occupancy_percent,
        })
    return results


# ---------------------------------------------------------------------
# Most booked movies
# ---------------------------------------------------------------------

def most_booked_movies(start_dt, end_exclusive, limit=10):
    return list(
        Movie.objects.filter(
            booking__booked_at__gte=start_dt, booking__booked_at__lt=end_exclusive,
        )
        .values('id', 'name')
        .annotate(booking_count=Count('booking'))
        .order_by('-booking_count')[:limit]
    )


# ---------------------------------------------------------------------
# Top-performing theaters - booking counts AND revenue, merged safely
# ---------------------------------------------------------------------

def top_performing_theaters(start_dt, end_exclusive, limit=10):
    """
    Two independent grouped queries - Booking counts by theater, Payment
    revenue by theater - merged by theater_id in Python. Never a single
    query joining Payment and Booking for the same theater, which would
    fan out and multiply revenue by the number of seats in that payment.
    """
    booking_counts = {
        row['theater_id']: row['booking_count']
        for row in (
            Booking.objects.filter(
                booked_at__gte=start_dt, booked_at__lt=end_exclusive,
            )
            .values('theater_id')
            .annotate(booking_count=Count('id'))
        )
    }

    revenue_rows = {
        row['theater_id']: (row['gross'], row['net'])
        for row in (
            Payment.objects.filter(
                status__in=CAPTURED_STATUSES,
                created_at__gte=start_dt, created_at__lt=end_exclusive,
            )
            .values('theater_id')
            .annotate(
                gross=Coalesce(Sum('amount_rupees'), Decimal('0')),
                net=Coalesce(Sum(F('amount_rupees') - F('refunded_amount')), Decimal('0')),
            )
        )
    }

    theater_ids = set(booking_counts) | set(revenue_rows)
    theaters = {t.id: t for t in Theater.objects.filter(id__in=theater_ids).select_related('movie')}

    results = []
    for theater_id in theater_ids:
        theater = theaters.get(theater_id)
        if theater is None:
            continue
        gross, net = revenue_rows.get(theater_id, (Decimal('0'), Decimal('0')))
        results.append({
            'theater': theater,
            'booking_count': booking_counts.get(theater_id, 0),
            'gross_revenue': gross,
            'net_revenue': net,
        })

    results.sort(key=lambda r: r['gross_revenue'], reverse=True)
    return results[:limit]


# ---------------------------------------------------------------------
# Peak booking hours
# ---------------------------------------------------------------------

def peak_booking_hours(start_dt, end_exclusive):
    """Booking counts grouped by hour-of-day (0-23) within the selected range."""
    return list(
        Booking.objects.filter(
            booked_at__gte=start_dt, booked_at__lt=end_exclusive,
        )
        .annotate(hour=ExtractHour('booked_at'))
        .values('hour')
        .annotate(count=Count('id'))
        .order_by('hour')
    )


# ---------------------------------------------------------------------
# Cancellation / refund statistics
# ---------------------------------------------------------------------

def cancellation_and_refund_stats(start_dt, end_exclusive):
    payment_qs = Payment.objects.filter(
        created_at__gte=start_dt, created_at__lt=end_exclusive,
    )

    cancelled_count = payment_qs.filter(status=PaymentStatus.CANCELLED).count()

    refund_aggregate = payment_qs.filter(
        refund_status__in=[RefundStatus.PENDING, RefundStatus.COMPLETED, RefundStatus.PARTIAL]
    ).aggregate(
        refund_pending_count=Count('id', filter=Q(refund_status=RefundStatus.PENDING)),
        refund_completed_count=Count('id', filter=Q(refund_status=RefundStatus.COMPLETED)),
        refund_partial_count=Count('id', filter=Q(refund_status=RefundStatus.PARTIAL)),
        total_refunded_amount=Coalesce(Sum('refunded_amount'), Decimal('0')),
    )

    return {
        'cancelled_count': cancelled_count,
        'refund_pending_count': refund_aggregate['refund_pending_count'],
        'refund_completed_count': refund_aggregate['refund_completed_count'],
        'refund_partial_count': refund_aggregate['refund_partial_count'],
        'total_refunded_amount': refund_aggregate['total_refunded_amount'],
    }


# ---------------------------------------------------------------------
# User growth
# ---------------------------------------------------------------------

def user_growth(start_dt, end_exclusive):
    """New user signups per month within the selected range, via date_joined."""
    return list(
        User.objects.filter(
            date_joined__gte=start_dt, date_joined__lt=end_exclusive,
        )
        .annotate(month=TruncMonth('date_joined'))
        .values('month')
        .annotate(new_users=Count('id'))
        .order_by('month')
    )


# ---------------------------------------------------------------------
# One entry point both the HTML dashboard and the CSV export call, so
# there is exactly one implementation of every number shown either place.
# ---------------------------------------------------------------------

def build_dashboard_data(start_date, end_date):
    start_dt, end_exclusive = resolve_date_range(start_date, end_date)

    return {
        'start_date': start_date,
        'end_date': end_date,
        'summary_cards': revenue_summary_cards(),
        'selected_summary': selected_range_summary(start_dt, end_exclusive),
        'revenue_trend': revenue_trend(start_dt, end_exclusive),
        'booking_trend': booking_trend(start_dt, end_exclusive),
        'occupancy': occupancy_by_theater(start_dt, end_exclusive),
        'most_booked_movies': most_booked_movies(start_dt, end_exclusive),
        'top_theaters': top_performing_theaters(start_dt, end_exclusive),
        'peak_hours': peak_booking_hours(start_dt, end_exclusive),
        'cancellation_refund_stats': cancellation_and_refund_stats(start_dt, end_exclusive),
        'user_growth': user_growth(start_dt, end_exclusive),
    }