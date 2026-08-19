import csv
import json

import razorpay

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction, IntegrityError
from django.db.models import Avg, Count, Exists, Min, OuterRef, Q
from django.http import HttpResponse, Http404
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from datetime import timedelta, datetime

from .models import (
    Movie, Theater, Seat, Booking, RecentlyViewed, SeatReservation,
    Payment, WebhookEvent, Ticket,
    PaymentStatus, RefundStatus, WebhookStatus,
    MoviePoster, Review, ReviewReport,
)
from .services import (
    confirm_or_reconcile_payment,
    mark_payment_cancelled,
    find_reusable_created_payment,
    PaymentIntegrityMismatch,
)
from .ticket_utils import generate_ticket_pdf
from .dashboard_services import build_dashboard_data, default_date_range
from .forms import ReviewForm, ReviewReportForm


# =====================================================================
# TASK 1 — Movie Discovery (unchanged from the approved Task 1 version)
# =====================================================================

def get_recommended_movies(request):
    """
    Builds the 'Recommended for You' list.
    Primary signal: genres of movies the user has already booked.
    Secondary signal: genres of movies the user recently viewed (RecentlyViewed).
    Falls back to the most popular movies if the user has no history yet.
    Movies the user has already booked are excluded from the results.
    """
    if not request.user.is_authenticated:
        return Movie.objects.none()

    booked_movie_ids = Booking.objects.filter(user=request.user).values_list('movie_id', flat=True)

    booked_genres = Movie.objects.filter(id__in=booked_movie_ids).values_list('genre', flat=True)
    viewed_genres = RecentlyViewed.objects.filter(user=request.user).values_list('movie__genre', flat=True)

    # Combine both signals into one set of genre tokens (genre field can hold
    # comma-separated values, e.g. "Action, Thriller", so split on comma).
    genre_tokens = set()
    for genre_string in list(booked_genres) + list(viewed_genres):
        if genre_string:
            for token in genre_string.split(','):
                token = token.strip()
                if token:
                    genre_tokens.add(token)

    if genre_tokens:
        genre_query = Q()
        for token in genre_tokens:
            genre_query |= Q(genre__icontains=token)

        # Count('booking', ...) via the direct Movie -> Booking FK.
        # annotate()'s implicit GROUP BY on the movie's own columns already
        # guarantees unique movies, so an explicit .distinct() call is
        # unnecessary here.
        recommended_qs = (
            Movie.objects.filter(genre_query)
            .exclude(id__in=booked_movie_ids)
            .annotate(popularity=Count('booking', distinct=True))
            .order_by('-popularity')[:6]
        )
        # Materialize once with list(): checking truthiness on the list reuses
        # this single query, instead of .exists() running one query and then
        # the template loop re-running the whole queryset a second time.
        recommended_list = list(recommended_qs)
        if recommended_list:
            return recommended_list

    # Fallback: no booking/viewing history yet, or no genre matches found.
    return (
        Movie.objects.exclude(id__in=booked_movie_ids)
        .annotate(popularity=Count('booking', distinct=True))
        .order_by('-popularity')[:6]
    )


def movie_list(request):
    movies = Movie.objects.all()

    # --- Search by title ---
    search_query = request.GET.get('search')
    if search_query:
        movies = movies.filter(name__icontains=search_query)

    # --- Filters ---
    genre = request.GET.get('genre')
    if genre:
        movies = movies.filter(genre__icontains=genre)

    language = request.GET.get('language')
    if language:
        movies = movies.filter(language__icontains=language)

    release_date = request.GET.get('release_date')
    if release_date:
        movies = movies.filter(release_date=release_date)

    rating = request.GET.get('rating')
    if rating:
        movies = movies.filter(rating__gte=rating)

    # Filters that live on Theater (city, theater name, show timings) require
    # joining across the relation. IMPORTANT: each of these must be combined
    # into ONE filter() call (via a single Q object), not separate sequential
    # filter() calls, since Django creates an independent JOIN per filter()
    # call when spanning a multi-valued relationship.
    theater_conditions = Q()
    has_theater_filter = False

    city = request.GET.get('city')
    if city:
        theater_conditions &= Q(theaters__city__icontains=city)
        has_theater_filter = True

    theater_name = request.GET.get('theater')
    if theater_name:
        theater_conditions &= Q(theaters__name__icontains=theater_name)
        has_theater_filter = True

    show_date = request.GET.get('show_date')
    if show_date:
        theater_conditions &= Q(theaters__time__date=show_date)
        has_theater_filter = True

    show_time = request.GET.get('show_time')
    if show_time == 'morning':
        theater_conditions &= Q(theaters__time__time__gte='06:00', theaters__time__time__lte='11:59')
        has_theater_filter = True
    elif show_time == 'afternoon':
        theater_conditions &= Q(theaters__time__time__gte='12:00', theaters__time__time__lte='16:59')
        has_theater_filter = True
    elif show_time == 'evening':
        theater_conditions &= Q(theaters__time__time__gte='17:00', theaters__time__time__lte='20:59')
        has_theater_filter = True
    elif show_time == 'night':
        theater_conditions &= (Q(theaters__time__time__gte='21:00') | Q(theaters__time__time__lte='05:59'))
        has_theater_filter = True

    if has_theater_filter:
        movies = movies.filter(theater_conditions)

    # --- Annotations needed for sorting ---
    movies = movies.annotate(
        popularity=Count('booking', distinct=True),
        min_ticket_price=Min('theaters__ticket_price'),
    )

    # --- Sorting ---
    sort = request.GET.get('sort')
    if sort == 'popularity':
        movies = movies.order_by('-popularity')
    elif sort == 'newest':
        movies = movies.order_by('-release_date')
    elif sort == 'rating':
        movies = movies.order_by('-rating')
    elif sort == 'price':
        movies = movies.order_by('min_ticket_price')
    else:
        movies = movies.order_by('name')

    # --- Pagination ---
    paginator = Paginator(movies, 9)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    total_count = paginator.count

    # --- Recommended for You ---
    recommended_movies = get_recommended_movies(request)

    context = {
        'movies': page_obj,
        'page_obj': page_obj,
        'total_count': total_count,
        'recommended_movies': recommended_movies,
    }
    return render(request, 'movies/movie_list.html', context)


def theater_list(request, movie_id):
    movie = get_object_or_404(Movie, id=movie_id)
    theater = Theater.objects.filter(movie=movie)

    if request.user.is_authenticated:
        RecentlyViewed.objects.update_or_create(
            user=request.user,
            movie=movie,
        )

    return render(request, 'movies/theater_list.html', {'movie': movie, 'theaters': theater})


# =====================================================================
# TASK 5 — Smart Seat Reservation (unchanged from the approved version)
# =====================================================================

@login_required(login_url='/login/')
def book_seats(request, theater_id):
    theaters = get_object_or_404(Theater, id=theater_id)

    # Lazy expiry: drop any reservation rows whose 2-minute hold has already
    # passed. Plain DELETE, no select_for_update needed - deleting rows that
    # are already invalid is safe even if two requests race on it.
    SeatReservation.objects.filter(seat__theater=theaters, expires_at__lte=timezone.now()).delete()

    seats = Seat.objects.filter(theater=theaters).select_related('reservation')
    for seat in seats:
        try:
            seat.active_reservation = seat.reservation
        except SeatReservation.DoesNotExist:
            seat.active_reservation = None

    if request.method == 'POST':
        selected_Seats = request.POST.getlist('seats')
        if not selected_Seats:
            return render(request, "movies/seat_selection.html", {'theaters': theaters, "seats": seats, 'error': "No seat selected"})

        conflict_seats = []
        with transaction.atomic():
            locked_seats = list(
                Seat.objects.select_for_update().filter(id__in=selected_Seats, theater=theaters)
            )
            if len(locked_seats) != len(selected_Seats):
                return render(request, "movies/seat_selection.html", {'theaters': theaters, "seats": seats, 'error': "Invalid seat selection"})

            locked_reservations = list(
                SeatReservation.objects.select_for_update().filter(seat__in=locked_seats)
            )
            now = timezone.now()
            active_reservations = {}
            for res in locked_reservations:
                if res.expires_at <= now:
                    res.delete()
                else:
                    active_reservations[res.seat_id] = res

            for seat in locked_seats:
                if seat.is_booked:
                    conflict_seats.append(seat.seat_number)
                    continue
                existing = active_reservations.get(seat.id)
                if existing and existing.user_id != request.user.id:
                    conflict_seats.append(seat.seat_number)

            if conflict_seats:
                error_message = f"The following seats are no longer available: {', '.join(conflict_seats)}"
                return render(request, 'movies/seat_selection.html', {'theaters': theaters, "seats": seats, 'error': error_message})

            SeatReservation.objects.filter(
                user=request.user, seat__theater=theaters
            ).exclude(seat__in=locked_seats).delete()

            expires_at = now + timedelta(minutes=2)
            for seat in locked_seats:
                SeatReservation.objects.update_or_create(
                    seat=seat,
                    defaults={'user': request.user, 'expires_at': expires_at}
                )

        return redirect('reservation_confirmation', theater_id=theaters.id)

    return render(request, 'movies/seat_selection.html', {'theaters': theaters, "seats": seats})


@login_required(login_url='/login/')
def reservation_confirmation(request, theater_id):
    theater = get_object_or_404(Theater, id=theater_id)
    reservations = (
        SeatReservation.objects
        .filter(user=request.user, seat__theater=theater, expires_at__gt=timezone.now())
        .select_related('seat')
        .order_by('seat__seat_number')
    )
    if not reservations.exists():
        return redirect('book_seats', theater_id=theater.id)

    expires_at = reservations.first().expires_at
    context = {
        'theater': theater,
        'reservations': reservations,
        'expires_at': expires_at,
    }
    return render(request, 'movies/reservation_confirmation.html', context)


# =====================================================================
# TASK 4 — Razorpay Payment Workflow
# =====================================================================

def _get_razorpay_client():
    return razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))


@login_required(login_url='/login/')
def payment_placeholder(request, theater_id):
    """
    Creates (or reuses) a Razorpay order for the user's currently active
    SeatReservation rows on this theater/show, and renders the Razorpay
    Checkout page.

    Reuse behavior: if the user refreshes/reopens this page with the SAME
    active reservation set and amount, and their most recent attempt is
    still CREATED, find_reusable_created_payment() returns that existing
    Payment instead of creating a new Razorpay order - so refreshing does
    not spawn endless orders. A genuine retry after FAILED/CANCELLED is
    never matched by that lookup, so it always gets a brand new order.
    """
    theater = get_object_or_404(Theater, id=theater_id)
    reservations = (
        SeatReservation.objects
        .filter(user=request.user, seat__theater=theater, expires_at__gt=timezone.now())
        .select_related('seat')
        .order_by('seat__seat_number')
    )
    if not reservations.exists():
        return redirect('book_seats', theater_id=theater.id)

    seat_ids = list(reservations.values_list('seat_id', flat=True))
    seat_count = len(seat_ids)
    ticket_price = theater.ticket_price or 0
    amount_rupees = ticket_price * seat_count
    amount_paise = int(amount_rupees * 100)

    payment = find_reusable_created_payment(request.user, theater, seat_ids, amount_paise)

    if payment is None:
        client = _get_razorpay_client()
        razorpay_order = client.order.create({
            'amount': amount_paise,
            'currency': 'INR',
            'payment_capture': 1,
        })
        payment = Payment.objects.create(
            user=request.user,
            theater=theater,
            movie=theater.movie,
            razorpay_order_id=razorpay_order['id'],
            amount_rupees=amount_rupees,
            amount_paise=amount_paise,
            currency='INR',
            seat_snapshot=seat_ids,
        )

    context = {
        'theater': theater,
        'reservations': reservations,
        'payment': payment,
        'razorpay_key_id': settings.RAZORPAY_KEY_ID,
        'razorpay_order_id': payment.razorpay_order_id,
        'amount_paise': payment.amount_paise,
        'amount_rupees': payment.amount_rupees,
        'user_email': request.user.email,
        'user_name': request.user.username,
    }
    return render(request, 'movies/payment_placeholder.html', context)


@login_required(login_url='/login/')
@require_POST
def payment_callback(request, theater_id):
    """
    Browser-side callback from Razorpay Checkout. Verifies the HMAC
    signature using the SERVER-STORED order id (looked up by
    user+theater+posted order id, never the raw posted value trusted on
    its own), then fetches the full ground-truth payment entity directly
    from Razorpay's API. Contains no booking logic itself - all of that
    lives in confirm_or_reconcile_payment(), which this and the webhook
    view both call.
    """
    theater = get_object_or_404(Theater, id=theater_id)
    posted_order_id = request.POST.get('razorpay_order_id')
    razorpay_payment_id = request.POST.get('razorpay_payment_id')
    razorpay_signature = request.POST.get('razorpay_signature')

    if not (posted_order_id and razorpay_payment_id and razorpay_signature):
        return render(request, 'movies/payment_placeholder.html', {
            'theater': theater,
            'callback_error': 'Incomplete payment response received from Razorpay.',
        })

    # Look up the Payment row by the posted order id, but SCOPED to this
    # user and this theater - a mismatched/foreign order id simply won't
    # be found here.
    try:
        payment = Payment.objects.get(
            razorpay_order_id=posted_order_id, user=request.user, theater=theater
        )
    except Payment.DoesNotExist:
        return render(request, 'movies/payment_placeholder.html', {
            'theater': theater,
            'callback_error': 'No matching payment record found for this order.',
        })

    # Use the SERVER's own stored order id (not the posted value) for
    # signature verification from this point forward.
    server_order_id = payment.razorpay_order_id

    client = _get_razorpay_client()
    try:
        client.utility.verify_payment_signature({
            'razorpay_order_id': server_order_id,
            'razorpay_payment_id': razorpay_payment_id,
            'razorpay_signature': razorpay_signature,
        })
    except razorpay.errors.SignatureVerificationError:
        return render(request, 'movies/payment_placeholder.html', {
            'theater': theater,
            'callback_error': 'Payment signature verification failed. Please try again.',
        })

    payment.razorpay_signature = razorpay_signature
    payment.save(update_fields=['razorpay_signature'])

    # Signature is genuine. Fetch the FULL payment entity directly from
    # Razorpay (never trust status/amount/etc. reported by the browser),
    # OUTSIDE of any database transaction - this is a network call.
    razorpay_payment_entity = client.payment.fetch(razorpay_payment_id)

    try:
        outcome = confirm_or_reconcile_payment(server_order_id, razorpay_payment_entity)
    except PaymentIntegrityMismatch:
        outcome = 'integrity_mismatch'
    except IntegrityError:
        outcome = 'integrity_error'

    if outcome in ('confirmed', 'already_confirmed'):
        return redirect('profile')
    elif outcome == 'success_unfulfilled_refund_pending':
        return render(request, 'movies/payment_placeholder.html', {
            'theater': theater,
            'callback_error': (
                "Your payment was captured, but your seats were no longer "
                "available by the time it was confirmed. Our team will "
                "process a refund shortly — please contact support and "
                f"reference payment ID: {razorpay_payment_id}"
            ),
        })
    elif outcome in ('integrity_mismatch', 'integrity_error'):
        return render(request, 'movies/payment_placeholder.html', {
            'theater': theater,
            'callback_error': (
                "We couldn't verify your payment due to a data consistency "
                "issue. Please contact support and reference payment ID: "
                f"{razorpay_payment_id}"
            ),
        })
    else:
        return render(request, 'movies/payment_placeholder.html', {
            'theater': theater,
            'callback_error': "Payment was not successful. Please try again.",
        })


@login_required(login_url='/login/')
@require_POST
def payment_cancel(request, theater_id):
    """
    Called (via JS fetch) when the user explicitly closes/cancels the
    Razorpay Checkout widget. Ownership of the Payment (must belong to
    request.user AND this theater) is enforced inside
    mark_payment_cancelled() itself via the lookup's filter kwargs.
    """
    theater = get_object_or_404(Theater, id=theater_id)
    razorpay_order_id = request.POST.get('razorpay_order_id')
    if razorpay_order_id:
        try:
            mark_payment_cancelled(razorpay_order_id, user=request.user, theater_id=theater.id)
        except Payment.DoesNotExist:
            pass
    return redirect('book_seats', theater_id=theater.id)


@csrf_exempt
@require_POST
def razorpay_webhook(request):
    """
    Server-to-server Razorpay webhook endpoint. Verifies using the RAW
    request body + webhook secret. Uses the X-Razorpay-Event-Id HEADER for
    replay protection, checked BEFORE any payload parsing or business logic.

    A WebhookEvent row is created with status=RECEIVED as soon as the
    event is authenticated, and is only flipped to PROCESSED after
    confirm_or_reconcile_payment() completes without error. An UNEXPECTED
    failure returns HTTP 500 so Razorpay's own retry mechanism retries the
    same event_id - since this row is not PROCESSED, it will be allowed to
    try again rather than being silently blocked forever.
    """
    raw_body = request.body
    received_signature = request.headers.get('X-Razorpay-Signature', '')
    event_id = request.headers.get('X-Razorpay-Event-Id', '')

    if not event_id:
        return HttpResponse(status=400)

    client = _get_razorpay_client()
    try:
        client.utility.verify_webhook_signature(
            raw_body.decode('utf-8'), received_signature, settings.RAZORPAY_WEBHOOK_SECRET
        )
    except (razorpay.errors.SignatureVerificationError, UnicodeDecodeError):
        return HttpResponse(status=400)

    try:
        payload = json.loads(raw_body)
    except (ValueError, UnicodeDecodeError):
        return HttpResponse(status=400)
    event_type = payload.get('event', 'unknown')

    try:
        webhook_event, created = WebhookEvent.objects.get_or_create(
            razorpay_event_id=event_id,
            defaults={'event_type': event_type, 'payload': payload, 'status': WebhookStatus.RECEIVED},
        )
    except IntegrityError:
        # Another concurrent request won the race and already created this
        # exact razorpay_event_id (the unique constraint on
        # WebhookEvent.razorpay_event_id caught it). Re-fetch that row and
        # fall through to the normal replay logic below instead of crashing.
        webhook_event = WebhookEvent.objects.get(razorpay_event_id=event_id)
        created = False

    if not created and webhook_event.status == WebhookStatus.PROCESSED:
        # True replay of an already-successfully-processed event.
        return HttpResponse(status=200)

    payment_entity = payload.get('payload', {}).get('payment', {}).get('entity', {})
    razorpay_order_id = payment_entity.get('order_id')
    razorpay_payment_id = payment_entity.get('id')

    if not (razorpay_order_id and razorpay_payment_id):
        webhook_event.status = WebhookStatus.FAILED
        webhook_event.error_message = 'Webhook payload missing order_id or payment_id.'
        webhook_event.save()
        return HttpResponse(status=200)

    try:
        # Re-fetch the ground-truth entity directly from Razorpay - never
        # trust the webhook payload's own fields as final (this is what
        # makes late-authorization handling correct). This network call
        # happens BEFORE any database transaction is opened.
        razorpay_payment_entity = client.payment.fetch(razorpay_payment_id)
        confirm_or_reconcile_payment(razorpay_order_id, razorpay_payment_entity)
    except Payment.DoesNotExist as exc:
        webhook_event.status = WebhookStatus.FAILED
        webhook_event.error_message = f'No matching Payment for this order: {exc}'
        webhook_event.save()
        return HttpResponse(status=200)
    except (PaymentIntegrityMismatch, IntegrityError) as exc:
        webhook_event.status = WebhookStatus.FAILED
        webhook_event.error_message = str(exc)
        webhook_event.save()
        # A genuine data-integrity problem - a blind retry won't resolve
        # it, so acknowledge rather than have Razorpay hammer this endpoint.
        return HttpResponse(status=200)
    except Exception as exc:
        webhook_event.status = WebhookStatus.FAILED
        webhook_event.error_message = str(exc)
        webhook_event.save()
        return HttpResponse(status=500)

    webhook_event.status = WebhookStatus.PROCESSED
    webhook_event.processed_at = timezone.now()
    webhook_event.save()
    return HttpResponse(status=200)


# =====================================================================
# TASK 2 — Ticket download + QR verification
# =====================================================================

@login_required(login_url='/login/')
def download_ticket(request, ticket_id):
    """
    Login-protected ticket download. Verifies ticket.user == request.user
    before generating/returning anything - a mismatch raises Http404
    rather than a permission-denied page, so the existence of someone
    else's ticket ID isn't confirmed/denied differently.
    """
    ticket = get_object_or_404(Ticket, id=ticket_id)
    if ticket.user_id != request.user.id:
        raise Http404("Ticket not found.")

    pdf_buffer = generate_ticket_pdf(ticket)
    filename = f'{ticket.booking_reference}.pdf'
    response = HttpResponse(pdf_buffer.read(), content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


def verify_ticket(request, token):
    """
    Public QR-scan verification page. Shows only safe, non-sensitive
    fields (validity, booking reference, movie, theater, screen, show
    time, seats) - no user email, no payment amount, no payment ID.
    """
    try:
        ticket = Ticket.objects.get(verification_token=token)
        valid = True
    except Ticket.DoesNotExist:
        ticket = None
        valid = False

    return render(request, 'movies/ticket_verify.html', {'ticket': ticket, 'valid': valid})


# =====================================================================
# TASK 6 — Admin Dashboard (analytics live in movies/dashboard_services.py)
# =====================================================================

def _parse_dashboard_date_range(request):
    """
    Shared by admin_dashboard() and export_dashboard_csv() so both use the
    EXACT same start/end resolution - no duplicated parsing logic that
    could drift between the HTML view and the CSV export.

    Returns (start_date, end_date, error) where error is None on success,
    or a user-facing string if start/end were invalid/out of order.
    """
    start_raw = request.GET.get('start_date')
    end_raw = request.GET.get('end_date')

    if not start_raw or not end_raw:
        start_date, end_date = default_date_range()
        return start_date, end_date, None

    try:
        start_date = datetime.strptime(start_raw, '%Y-%m-%d').date()
        end_date = datetime.strptime(end_raw, '%Y-%m-%d').date()
    except ValueError:
        start_date, end_date = default_date_range()
        return start_date, end_date, "Invalid date format. Showing the default last-30-days range instead."

    if start_date > end_date:
        start_date, end_date = default_date_range()
        return start_date, end_date, "Start date must not be after end date. Showing the default last-30-days range instead."

    return start_date, end_date, None


@staff_member_required
def admin_dashboard(request):
    """
    Task 6: real-time business insights. Read-only - no Task 1/2/4/5
    behavior is touched by this view. All heavy aggregation happens in
    movies/dashboard_services.py; this view only resolves the requested
    date range and renders the results.
    """
    start_date, end_date, date_error = _parse_dashboard_date_range(request)
    dashboard_data = build_dashboard_data(start_date, end_date)
    dashboard_data['date_error'] = date_error

    return render(request, 'movies/admin_dashboard.html', dashboard_data)


@staff_member_required
def export_dashboard_csv(request):
    """
    Exports the SAME analytics as admin_dashboard(), for the SAME resolved
    date range, via the SAME build_dashboard_data() call - never a
    separately-implemented calculation. Writes only aggregated rows
    (summary/movie performance/theater performance/occupancy/
    cancellation-refund/user-growth), never raw Booking/Payment rows.
    """
    start_date, end_date, _date_error = _parse_dashboard_date_range(request)
    data = build_dashboard_data(start_date, end_date)

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = (
        f'attachment; filename="dashboard_{start_date}_to_{end_date}.csv"'
    )
    writer = csv.writer(response)

    writer.writerow([f'BookMySeat Admin Dashboard Export: {start_date} to {end_date}'])
    writer.writerow([])

    writer.writerow(['Summary (Selected Range)'])
    summary = data['selected_summary']
    writer.writerow(['Gross Revenue', summary['gross_revenue']])
    writer.writerow(['Net Revenue', summary['net_revenue']])
    writer.writerow(['Confirmed Bookings', summary['confirmed_bookings']])
    writer.writerow(['Cancellations', summary['cancellations']])
    writer.writerow(['Total Refunded', summary['total_refunded']])
    writer.writerow([])

    cards = data['summary_cards']
    writer.writerow(['Fixed-Period Revenue (Current Calendar Periods)'])
    writer.writerow(['Today', cards['today_gross']])
    writer.writerow(['This Week', cards['week_gross']])
    writer.writerow(['This Month', cards['month_gross']])
    writer.writerow(['This Year', cards['year_gross']])
    writer.writerow([])

    writer.writerow(['Revenue Trend (Daily)'])
    writer.writerow(['Date', 'Gross Revenue'])
    for row in data['revenue_trend']:
        writer.writerow([row['day'], row['gross']])
    writer.writerow([])

    writer.writerow(['Booking Trend (Daily)'])
    writer.writerow(['Date', 'Confirmed Bookings'])
    for row in data['booking_trend']:
        writer.writerow([row['day'], row['count']])
    writer.writerow([])

    writer.writerow(['Occupancy By Theater'])
    writer.writerow(['Theater', 'Movie', 'Confirmed Bookings', 'Total Seats', 'Occupancy %'])
    for row in data['occupancy']:
        writer.writerow([
            row['theater'].name, row['theater'].movie.name,
            row['confirmed_bookings'], row['total_seats'], row['occupancy_percent'],
        ])
    writer.writerow([])

    writer.writerow(['Most Booked Movies'])
    writer.writerow(['Movie', 'Booking Count'])
    for row in data['most_booked_movies']:
        writer.writerow([row['name'], row['booking_count']])
    writer.writerow([])

    writer.writerow(['Top-Performing Theaters'])
    writer.writerow(['Theater', 'Movie', 'Booking Count', 'Gross Revenue', 'Net Revenue'])
    for row in data['top_theaters']:
        writer.writerow([
            row['theater'].name, row['theater'].movie.name,
            row['booking_count'], row['gross_revenue'], row['net_revenue'],
        ])
    writer.writerow([])

    writer.writerow(['Peak Booking Hours'])
    writer.writerow(['Hour (24h)', 'Booking Count'])
    for row in data['peak_hours']:
        writer.writerow([row['hour'], row['count']])
    writer.writerow([])

    writer.writerow(['Cancellation / Refund Statistics'])
    stats = data['cancellation_refund_stats']
    writer.writerow(['Cancelled Payments', stats['cancelled_count']])
    writer.writerow(['Refunds Pending', stats['refund_pending_count']])
    writer.writerow(['Refunds Completed', stats['refund_completed_count']])
    writer.writerow(['Refunds Partial', stats['refund_partial_count']])
    writer.writerow(['Total Refunded Amount', stats['total_refunded_amount']])
    writer.writerow([])

    writer.writerow(['User Growth (Monthly)'])
    writer.writerow(['Month', 'New Users'])
    for row in data['user_growth']:
        writer.writerow([row['month'], row['new_users']])

    return response


# =====================================================================
# TASK 3 — Movie Management with Trailer, Reviews and Ratings
# =====================================================================

def _similar_movies(movie, limit=6):
    """
    Same/similar genre (using the same comma-token approach as Task 1's
    recommendation engine) OR same language, excluding the current movie.
    One filtered query, no Python-side comparison loop. Handles a blank
    genre/language cleanly by simply contributing no condition for that
    signal rather than erroring.
    """
    genre_tokens = []
    if movie.genre:
        genre_tokens = [token.strip() for token in movie.genre.split(',') if token.strip()]

    query = Q()
    has_condition = False
    for token in genre_tokens:
        query |= Q(genre__icontains=token)
        has_condition = True
    if movie.language:
        query |= Q(language=movie.language)
        has_condition = True

    if not has_condition:
        return Movie.objects.none()

    return (
        Movie.objects.filter(query)
        .exclude(id=movie.id)
        .distinct()
        .order_by('-release_date')[:limit]
    )


def _trending_movies(limit=6):
    """Booking count over the last 7 days, via a single annotated query."""
    cutoff = timezone.now() - timedelta(days=7)
    return (
        Movie.objects.annotate(
            recent_bookings=Count('booking', filter=Q(booking__booked_at__gte=cutoff))
        )
        .filter(recent_bookings__gt=0)
        .order_by('-recent_bookings')[:limit]
    )


def _recently_released_movies(exclude_movie=None, limit=6):
    qs = Movie.objects.filter(release_date__lte=timezone.localdate()).order_by('-release_date')
    if exclude_movie is not None:
        qs = qs.exclude(id=exclude_movie.id)
    return qs[:limit]


def movie_detail(request, movie_id):
    """
    Public movie details page. Does not modify theater_list - this is a
    separate route/view, reachable via a new "Details" link (Part 3),
    while theater_list remains the existing booking-flow entry point.
    """
    movie = get_object_or_404(
        Movie.objects.prefetch_related('posters'),
        id=movie_id,
    )

    # Same safe update_or_create pattern already used in theater_list -
    # theater_list itself is untouched.
    if request.user.is_authenticated:
        RecentlyViewed.objects.update_or_create(user=request.user, movie=movie)

    now = timezone.now()

    # Verified Viewer badge: computed via Exists(OuterRef(...)) against
    # Booking, annotated onto each review row in ONE query - never a
    # stored field, never a per-row Booking query.
    verified_subquery = Booking.objects.filter(
        user_id=OuterRef('user_id'),
        movie_id=movie.id,
        theater__time__lte=now,
    )

    reviews = (
        Review.objects.filter(movie=movie)
        .select_related('user')
        .annotate(is_verified_viewer=Exists(verified_subquery))
        .order_by('-created_at')
    )
    review_count = reviews.count()

    # Task 3's own average, computed via Avg() - Movie.rating (Task 1) is
    # never read from or written to here.
    average_review_rating = Review.objects.filter(movie=movie).aggregate(
        avg=Avg('rating')
    )['avg']

    can_review = False
    user_review = None
    review_form = None
    if request.user.is_authenticated:
        user_review = Review.objects.filter(user=request.user, movie=movie).first()
        if user_review is None:
            can_review = Booking.objects.filter(
                user=request.user, movie=movie, theater__time__lte=now,
            ).exists()
            if can_review:
                review_form = ReviewForm()

    report_form = ReviewReportForm()

    context = {
        'movie': movie,
        'posters': movie.posters.all(),
        'reviews': reviews,
        'average_review_rating': average_review_rating,
        'review_count': review_count,
        'can_review': can_review,
        'user_review': user_review,
        'review_form': review_form,
        'report_form': report_form,
        'similar_movies': _similar_movies(movie),
        'trending_movies': _trending_movies(),
        'recently_released': _recently_released_movies(exclude_movie=movie),
    }
    return render(request, 'movies/movie_detail.html', context)


@login_required(login_url='/login/')
@require_POST
def create_review(request, movie_id):
    """
    Eligibility and ownership are always re-checked server-side here -
    movie_id comes from the URL (not trusted POST data), and user is
    always request.user, never a posted field.
    """
    movie = get_object_or_404(Movie, id=movie_id)

    is_eligible = Booking.objects.filter(
        user=request.user, movie=movie, theater__time__lte=timezone.now(),
    ).exists()
    if not is_eligible:
        messages.error(request, "You can only review a movie after attending a booked show.")
        return redirect('movie_detail', movie_id=movie.id)

    if Review.objects.filter(user=request.user, movie=movie).exists():
        messages.error(request, "You have already reviewed this movie.")
        return redirect('movie_detail', movie_id=movie.id)

    form = ReviewForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Please correct the errors in your review.")
        return redirect('movie_detail', movie_id=movie.id)

    review = form.save(commit=False)
    review.user = request.user
    review.movie = movie
    try:
        review.save()
    except IntegrityError:
        # Lost a race against another request creating the same
        # (user, movie) review at the same instant - the UniqueConstraint
        # caught it. Same user-facing outcome as the ordinary duplicate check.
        messages.error(request, "You have already reviewed this movie.")
        return redirect('movie_detail', movie_id=movie.id)

    messages.success(request, "Your review has been posted.")
    return redirect('movie_detail', movie_id=movie.id)


@login_required(login_url='/login/')
@require_POST
def edit_review(request, review_id):
    """
    The lookup is scoped to user=request.user - a mismatch raises 404,
    same ownership pattern already used by download_ticket (Task 2) and
    mark_payment_cancelled (Task 4). No other user's review can ever be
    reached by this view. user/movie are never editable via the form.
    """
    review = get_object_or_404(Review, id=review_id, user=request.user)

    form = ReviewForm(request.POST, instance=review)
    if not form.is_valid():
        messages.error(request, "Please correct the errors in your review.")
        return redirect('movie_detail', movie_id=review.movie_id)

    form.save()
    messages.success(request, "Your review has been updated.")
    return redirect('movie_detail', movie_id=review.movie_id)


@login_required(login_url='/login/')
@require_POST
def report_review(request, review_id):
    """
    Reporting never deletes or hides the underlying Review - it only ever
    creates a ReviewReport row for admin triage. Self-reporting and
    duplicate reports are both rejected.
    """
    review = get_object_or_404(Review, id=review_id)

    if review.user_id == request.user.id:
        messages.error(request, "You cannot report your own review.")
        return redirect('movie_detail', movie_id=review.movie_id)

    if ReviewReport.objects.filter(review=review, reported_by=request.user).exists():
        messages.error(request, "You have already reported this review.")
        return redirect('movie_detail', movie_id=review.movie_id)

    form = ReviewReportForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Please provide a reason for the report.")
        return redirect('movie_detail', movie_id=review.movie_id)

    report = form.save(commit=False)
    report.review = review
    report.reported_by = request.user
    try:
        report.save()
    except IntegrityError:
        # Lost a race against another duplicate-report attempt - the
        # UniqueConstraint caught it. Same outcome as the ordinary check.
        messages.error(request, "You have already reported this review.")
        return redirect('movie_detail', movie_id=review.movie_id)

    messages.success(request, "Thank you - this review has been reported for moderation.")
    return redirect('movie_detail', movie_id=review.movie_id)