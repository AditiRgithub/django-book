import uuid

from django.db import models
from django.contrib.auth.models import User
from django.core.validators import MinValueValidator, MaxValueValidator, RegexValidator
from django.utils import timezone


class Movie(models.Model):
    name = models.CharField(max_length=255)
    image = models.ImageField(upload_to="movies/")
    rating = models.DecimalField(max_digits=3, decimal_places=1, db_index=True)
    cast = models.TextField()
    description = models.TextField(blank=True, null=True)  # optional

    # --- New fields for Task 1: Movie Discovery ---
    genre = models.CharField(max_length=100, blank=True, null=True)
    language = models.CharField(max_length=50, blank=True, null=True)
    release_date = models.DateField(blank=True, null=True, db_index=True)

    # --- New fields for Task 3: Movie Management with Trailer, Reviews and Ratings ---
    # Only the extracted 11-character YouTube video ID is ever stored - never
    # a raw URL or iframe HTML. The regex below matches YouTube's own video
    # ID character set (A-Z, a-z, 0-9, underscore, hyphen) at exactly 11
    # characters. Full URL parsing/extraction happens in Part 2's form
    # layer; this validator is the model-level backstop that guarantees the
    # stored value can never be anything other than an ID-shaped string,
    # regardless of how it got there.
    youtube_video_id = models.CharField(
        max_length=11,
        blank=True,
        null=True,
        validators=[RegexValidator(
            regex=r'^[A-Za-z0-9_-]{11}$',
            message='Must be exactly 11 characters from A-Z, a-z, 0-9, "_", or "-" (a YouTube video ID, not a URL).',
        )],
    )
    age_certification = models.CharField(max_length=10, blank=True, null=True)
    duration_minutes = models.PositiveIntegerField(blank=True, null=True)

    def __str__(self):
        return self.name


class Theater(models.Model):
    name = models.CharField(max_length=255)
    movie = models.ForeignKey(Movie, on_delete=models.CASCADE, related_name='theaters')
    time = models.DateTimeField(db_index=True)

    # --- New fields for Task 1: Movie Discovery ---
    city = models.CharField(max_length=100, blank=True, null=True)
    ticket_price = models.DecimalField(max_digits=6, decimal_places=2, blank=True, null=True)

    # --- New field for Task 2: ticket PDF needs a screen ---
    screen = models.CharField(max_length=50, blank=True, default='Screen 1')

    def __str__(self):
        return f'{self.name} - {self.movie.name} at {self.time}'


class Seat(models.Model):
    theater = models.ForeignKey(Theater, on_delete=models.CASCADE, related_name='seats')
    seat_number = models.CharField(max_length=10)
    is_booked = models.BooleanField(default=False)

    def __str__(self):
        return f'{self.seat_number} in {self.theater.name}'


# --- Task 4: canonical status enums, used consistently across
# models / services / views / templates / admin. ---
class PaymentStatus(models.TextChoices):
    CREATED = 'created', 'Created'
    ATTEMPTED = 'attempted', 'Attempted'
    SUCCESS = 'success', 'Success (Captured & Fulfilled)'
    FAILED = 'failed', 'Failed'
    CANCELLED = 'cancelled', 'Cancelled'
    SUCCESS_UNFULFILLED = 'success_unfulfilled', 'Success (Unfulfilled - Refund Required)'
    REFUNDED = 'refunded', 'Refunded'
    PARTIALLY_REFUNDED = 'partially_refunded', 'Partially Refunded'


class RefundStatus(models.TextChoices):
    NOT_APPLICABLE = 'not_applicable', 'Not Applicable'
    PENDING = 'pending', 'Pending'
    COMPLETED = 'completed', 'Completed'
    PARTIAL = 'partial', 'Partial'


class WebhookStatus(models.TextChoices):
    RECEIVED = 'received', 'Received'
    PROCESSED = 'processed', 'Processed'
    FAILED = 'failed', 'Failed'


# --- New model for Task 4: Razorpay payment tracking ---
class Payment(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='payments')
    theater = models.ForeignKey(Theater, on_delete=models.CASCADE, related_name='payments')
    movie = models.ForeignKey(Movie, on_delete=models.CASCADE, related_name='payments')

    # unique=True already creates a database index - no redundant db_index=True.
    razorpay_order_id = models.CharField(max_length=100, unique=True)
    razorpay_payment_id = models.CharField(max_length=100, null=True, blank=True, unique=True)
    razorpay_signature = models.CharField(max_length=255, null=True, blank=True)

    amount_rupees = models.DecimalField(max_digits=10, decimal_places=2)
    amount_paise = models.PositiveBigIntegerField()
    currency = models.CharField(max_length=10, default='INR')

    status = models.CharField(max_length=25, choices=PaymentStatus.choices, default=PaymentStatus.CREATED)
    refund_status = models.CharField(max_length=20, choices=RefundStatus.choices, default=RefundStatus.NOT_APPLICABLE)
    refunded_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # Snapshot of the Seat IDs this specific payment attempt covers, taken
    # at order-creation time. The confirmation service trusts THIS list,
    # not whatever SeatReservation rows happen to exist when payment lands.
    seat_snapshot = models.JSONField()

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['status']),
            # Task 6: composite index for the dashboard's repeated
            # status__in=[...] + created_at range queries (revenue,
            # cancellations, refunds). Additive alongside the existing
            # single-column status index above - not a replacement.
            models.Index(fields=['status', 'created_at'], name='payment_status_created_idx'),
        ]

    def __str__(self):
        return f'Payment {self.razorpay_order_id} ({self.status}) by {self.user.username}'


# --- New model for Task 4: webhook replay protection ---
class WebhookEvent(models.Model):
    """
    One row per Razorpay webhook delivery, keyed on the X-Razorpay-Event-Id
    HEADER (not any ID inside the JSON payload). status/processed_at/
    error_message exist specifically so a processing failure does NOT
    permanently block Razorpay's retry mechanism: the row is created with
    status=RECEIVED as soon as the event is authenticated, and is only
    flipped to PROCESSED after confirm_or_reconcile_payment() completes
    without error. If processing raises, the row is left as FAILED (with
    the error recorded) - a retried delivery of the SAME event_id will
    find this row but see status != PROCESSED, so it will be allowed to
    try again rather than being silently treated as "already handled."
    """
    razorpay_event_id = models.CharField(max_length=100, unique=True)
    event_type = models.CharField(max_length=50)
    payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=WebhookStatus.choices, default=WebhookStatus.RECEIVED)
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(null=True, blank=True)

    def __str__(self):
        return f'{self.event_type} ({self.razorpay_event_id}) - {self.status}'


class Booking(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    seat = models.OneToOneField(Seat, on_delete=models.CASCADE)
    movie = models.ForeignKey(Movie, on_delete=models.CASCADE)
    theater = models.ForeignKey(Theater, on_delete=models.CASCADE)
    booked_at = models.DateTimeField(auto_now_add=True, db_index=True)

    # Explicitly groups every per-seat Booking row created by the same
    # successful payment. Nullable so it's compatible with any bookings
    # that predate Task 4. Never inferred from timestamps.
    payment = models.ForeignKey(Payment, on_delete=models.SET_NULL, null=True, blank=True, related_name='bookings')

    def __str__(self):
        return f'Booking by {self.user.username} for {self.seat.seat_number}'


# --- New model for Task 2: one ticket per successful Payment (not per seat) ---
class Ticket(models.Model):
    class EmailStatus(models.TextChoices):
        PENDING = 'pending', 'Pending'
        SENDING = 'sending', 'Sending'
        SENT = 'sent', 'Sent'
        FAILED = 'failed', 'Failed'

    # OneToOneField guarantees at most one Ticket per Payment at the
    # database level - the same idempotency pattern used for
    # SeatReservation.seat in Task 5.
    payment = models.OneToOneField(Payment, on_delete=models.CASCADE, related_name='ticket')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='tickets')

    verification_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    booking_reference = models.CharField(max_length=20, unique=True, editable=False, blank=True)

    # --- Immutable booking-time snapshot fields (correction #4) ---
    # Populated exactly once when the Ticket is created, right after
    # payment success. generate_ticket_pdf() and the email task read
    # ONLY these fields (never live Movie/Theater fields), so a later
    # admin edit to a Theater's name/screen/time, or to a Movie's name,
    # does not silently alter a ticket that was already issued.
    snapshot_movie_name = models.CharField(max_length=255)
    snapshot_theater_name = models.CharField(max_length=255)
    snapshot_screen = models.CharField(max_length=50, blank=True)
    snapshot_show_time = models.DateTimeField()
    snapshot_seat_numbers = models.JSONField(default=list)
    snapshot_amount_rupees = models.DecimalField(max_digits=10, decimal_places=2)
    snapshot_payment_reference = models.CharField(max_length=100, blank=True, null=True)

    email_status = models.CharField(max_length=10, choices=EmailStatus.choices, default=EmailStatus.PENDING)
    email_status_updated_at = models.DateTimeField(null=True, blank=True)
    email_attempts = models.PositiveIntegerField(default=0)
    emailed_at = models.DateTimeField(null=True, blank=True)
    last_email_error = models.TextField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if not self.booking_reference:
            # A fresh UUID4 segment - stable once set, unique, human-
            # readable enough for a ticket. Never derived from timestamps.
            self.booking_reference = f'BMS-{uuid.uuid4().hex[:10].upper()}'
        super().save(*args, **kwargs)

    def __str__(self):
        return f'Ticket {self.booking_reference} for {self.user.username}'


# --- New model for Task 1: "Recommended for You" (recently viewed signal) ---
class RecentlyViewed(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    movie = models.ForeignKey(Movie, on_delete=models.CASCADE)
    viewed_at = models.DateTimeField(auto_now=True)

    class Meta:
        # Guarantees one row per (user, movie) at the database level.
        # views.py uses update_or_create() so a repeat view updates
        # viewed_at on the existing row instead of inserting a new one.
        unique_together = ('user', 'movie')

    def __str__(self):
        return f'{self.user.username} viewed {self.movie.name}'


# --- New model for Task 5: Smart Seat Reservation with Live Availability ---
class SeatReservation(models.Model):
    """
    A temporary (2-minute) hold on a seat, created when a user selects seats
    and before payment is completed. This is intentionally separate from
    Seat.is_booked / Booking, which represent a PERMANENTLY confirmed seat
    after successful payment (Task 4).

    seat is a OneToOneField (not a plain ForeignKey) so the database itself
    enforces "at most one active reservation row per seat" via a unique
    constraint — a backstop in addition to select_for_update() locking.
    """
    seat = models.OneToOneField(Seat, on_delete=models.CASCADE, related_name='reservation')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='seat_reservations')
    reserved_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    def is_expired(self):
        return timezone.now() >= self.expires_at

    def __str__(self):
        return f'{self.seat} reserved by {self.user.username} until {self.expires_at}'


# =====================================================================
# TASK 3 — Movie Management with Trailer, Reviews and Ratings
# =====================================================================

class MoviePoster(models.Model):
    """
    Additional poster images for a movie. Movie.image (Task 1) remains the
    primary/fallback poster used everywhere it's already referenced - this
    model is purely additive, for a details-page gallery.
    """
    movie = models.ForeignKey(Movie, on_delete=models.CASCADE, related_name='posters')
    image = models.ImageField(upload_to='movies/posters/')
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['order', 'id']

    def __str__(self):
        return f'Poster for {self.movie.name} (order {self.order})'


class Review(models.Model):
    """
    One review per (user, movie), enforced at the database level via the
    UniqueConstraint below - not just an application-level check. Rating
    is validated 1-10 via MinValueValidator/MaxValueValidator, not trusted
    from unvalidated input.

    Deliberately has NO stored "verified" field: the Verified Viewer badge
    is always computed from live Booking/Theater facts at query time
    (Part 2), never trusted from a value stored on this row.
    """
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='reviews')
    movie = models.ForeignKey(Movie, on_delete=models.CASCADE, related_name='reviews')

    rating = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(10)]
    )
    review_text = models.TextField()

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['user', 'movie'], name='unique_review_per_user_movie'),
        ]
        indexes = [
            models.Index(fields=['movie', 'created_at'], name='review_movie_created_idx'),
        ]

    def __str__(self):
        return f'{self.rating}/10 review of {self.movie.name} by {self.user.username}'


class ReviewReport(models.Model):
    """
    User-submitted report of an inappropriate review. Reporting NEVER
    auto-deletes the underlying Review - status is a manual admin
    triage field only. One report per (review, reported_by), enforced at
    the database level.
    """
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        REVIEWED = 'reviewed', 'Reviewed'
        DISMISSED = 'dismissed', 'Dismissed'

    review = models.ForeignKey(Review, on_delete=models.CASCADE, related_name='reports')
    reported_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='review_reports')
    reason = models.CharField(max_length=255)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['review', 'reported_by'], name='unique_report_per_user_review'),
        ]

    def __str__(self):
        return f'Report on review #{self.review_id} by {self.reported_by.username} ({self.status})'