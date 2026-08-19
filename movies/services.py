"""
Task 4 + Task 2: shared, idempotent payment confirmation / reconciliation
logic, plus the payment-reuse helper and post-success Ticket creation.

Both the browser-callback view and the webhook view call
confirm_or_reconcile_payment() with a Razorpay-VERIFIED, FRESHLY-FETCHED
payment entity (the full dict from client.payment.fetch(...), never a
status string alone, and never the self-reported body from a webhook
payload or browser callback). Fetching from Razorpay is a network call
and MUST happen in the CALLER, outside any atomic() block - this module
performs no network I/O of its own.
"""
import logging

from django.db import transaction, IntegrityError
from django.utils import timezone

from .models import Payment, Seat, SeatReservation, Booking, Ticket, PaymentStatus, RefundStatus
from .tasks import send_ticket_email_task

logger = logging.getLogger(__name__)


class PaymentIntegrityMismatch(Exception):
    """
    Raised whenever data that should be internally consistent isn't:
      - the fetched Razorpay entity doesn't match the local Payment row
        it's supposedly for (wrong order_id / amount / currency / payment_id)
      - a Payment marked SUCCESS doesn't have matching Booking rows
      - a Payment's seat_snapshot references seats that don't exist or
        don't belong to its theater
    None of these should be reachable under normal operation given the
    locking and checks below; if one is ever raised, it's a bug or
    tampering attempt worth surfacing loudly, not a case to silently
    paper over as a harmless no-op.
    """
    pass


def find_reusable_created_payment(user, theater, seat_ids, amount_paise):
    """
    Task 4 retry/refresh behavior: returns an existing Payment to reuse if
    the user reloads/reopens the payment page with the SAME active
    reservation set and amount, and their most recent attempt for this
    theater is still CREATED (no completed attempt yet). This prevents
    spawning a new Razorpay order on every page refresh.

    Returns None if there's nothing safely reusable - which is always the
    case after a FAILED/CANCELLED attempt, since those are excluded by the
    status filter below, so a genuine retry always gets a brand new
    Payment/order (old attempts remain untouched in payment history).

    Pure DB read - no Razorpay network call.
    """
    existing = (
        Payment.objects.filter(user=user, theater=theater, status=PaymentStatus.CREATED)
        .order_by('-created_at')
        .first()
    )
    if existing and set(existing.seat_snapshot) == set(seat_ids) and existing.amount_paise == amount_paise:
        return existing
    return None


def _build_ticket_snapshot_defaults(payment, seat_numbers, entity_payment_id=None):
    """
    Builds the immutable, booking-time snapshot stored on Ticket creation
    (correction #4). Populated exactly once - every subsequent PDF/email
    generation reads ONLY these fields, never live Movie/Theater rows, so
    a later admin edit to a theater's name/screen/time can never alter a
    ticket that was already issued.
    """
    return {
        'user': payment.user,
        'snapshot_movie_name': payment.movie.name,
        'snapshot_theater_name': payment.theater.name,
        'snapshot_screen': payment.theater.screen,
        'snapshot_show_time': payment.theater.time,
        'snapshot_seat_numbers': sorted(seat_numbers),
        'snapshot_amount_rupees': payment.amount_rupees,
        'snapshot_payment_reference': entity_payment_id or payment.razorpay_payment_id,
    }


def _dispatch_ticket_email(ticket_id):
    """
    Correction #2: post-commit Celery dispatch must never make a
    committed successful booking look like it failed. This function runs
    from transaction.on_commit(), i.e. AFTER the Booking/Payment/Ticket
    transaction has already committed - by this point the booking is a
    permanent, successful fact no matter what happens below.

    If publishing to the broker fails (e.g. Redis unreachable), that
    failure is caught and logged here, and the Ticket is marked FAILED
    with the error recorded for later retry/inspection - it is NEVER
    allowed to propagate up and make the HTTP response (which already
    returned success to the browser/webhook caller by this point in the
    request lifecycle) look like an error.
    """
    try:
        send_ticket_email_task.delay(ticket_id)
    except Exception as exc:
        logger.exception('Failed to enqueue send_ticket_email_task for ticket %s', ticket_id)
        try:
            Ticket.objects.filter(id=ticket_id).update(
                email_status=Ticket.EmailStatus.FAILED,
                email_status_updated_at=timezone.now(),
                last_email_error=f'Failed to queue email task: {exc}',
            )
        except Exception:
            # Even the failure-recording write failed (e.g. DB briefly
            # down) - log and give up; the booking itself is unaffected
            # either way, since this all runs after commit.
            logger.exception('Also failed to record queue-failure state for ticket %s', ticket_id)


def confirm_or_reconcile_payment(razorpay_order_id, razorpay_payment_entity):
    """
    razorpay_order_id: the SERVER-STORED order id the caller looked up
    (never a client-supplied value used blindly).

    razorpay_payment_entity: the CALLER's freshly-fetched full Razorpay
    payment object, e.g. client.payment.fetch(razorpay_payment_id). Must
    contain at least: id, order_id, amount, currency, status, captured.

    Handles late authorization correctly: a Payment already marked FAILED
    or CANCELLED locally is NOT treated as permanently terminal here - if
    Razorpay later reports 'captured' for the same payment, this function
    still attempts to honor it. Only SUCCESS (verified against matching
    Booking rows) and SUCCESS_UNFULFILLED are true dead ends.

    On genuine success, also creates the Task 2 Ticket (one per Payment,
    enforced by Ticket.payment being a OneToOneField), populates its
    immutable snapshot fields, and schedules the async email via
    transaction.on_commit() so the Celery task is only queued after this
    transaction actually commits - never before.

    Returns a short outcome string for the caller to log or show to the user:
      'confirmed', 'already_confirmed', 'already_unfulfilled',
      'success_unfulfilled_refund_pending', 'failed', 'pending'

    Raises PaymentIntegrityMismatch or django.db.IntegrityError on genuine
    data-integrity problems - callers must NOT treat these as success.
    """
    entity_payment_id = razorpay_payment_entity.get('id')
    entity_order_id = razorpay_payment_entity.get('order_id')
    entity_amount = razorpay_payment_entity.get('amount')
    entity_currency = razorpay_payment_entity.get('currency')
    entity_status = razorpay_payment_entity.get('status')
    # Correction #3: FAIL CLOSED. Missing/false 'captured' must NOT be
    # treated as captured - only an explicit True counts. No default of
    # True on a missing key.
    entity_captured = razorpay_payment_entity.get('captured')

    with transaction.atomic():
        # Lock the Payment row first - fixed lock ordering (Payment, then
        # Seat, then SeatReservation) matches Task 5's Seat-before-
        # Reservation convention and avoids cross-flow deadlocks.
        payment = Payment.objects.select_for_update().get(razorpay_order_id=razorpay_order_id)

        # --- Cross-check the fetched entity actually belongs to THIS Payment ---
        if entity_order_id != payment.razorpay_order_id:
            raise PaymentIntegrityMismatch(
                f"Fetched Razorpay entity order_id '{entity_order_id}' does not "
                f"match local Payment.razorpay_order_id '{payment.razorpay_order_id}'."
            )
        if entity_amount != payment.amount_paise:
            raise PaymentIntegrityMismatch(
                f"Fetched Razorpay entity amount {entity_amount} does not match "
                f"local Payment.amount_paise {payment.amount_paise}."
            )
        if entity_currency != payment.currency:
            raise PaymentIntegrityMismatch(
                f"Fetched Razorpay entity currency '{entity_currency}' does not "
                f"match local Payment.currency '{payment.currency}'."
            )
        if (
            entity_payment_id
            and payment.razorpay_payment_id
            and entity_payment_id != payment.razorpay_payment_id
        ):
            raise PaymentIntegrityMismatch(
                f"Fetched Razorpay entity id '{entity_payment_id}' does not match "
                f"previously recorded Payment.razorpay_payment_id "
                f"'{payment.razorpay_payment_id}'."
            )

        # --- Genuine idempotency: already fulfilled, PROVEN by matching bookings ---
        if payment.status == PaymentStatus.SUCCESS:
            expected_seat_ids = set(payment.seat_snapshot)
            actual_seat_ids = set(payment.bookings.values_list('seat_id', flat=True))
            if actual_seat_ids == expected_seat_ids:
                # Defensive: ensure the Ticket exists even if an earlier
                # run somehow succeeded on bookings but failed before
                # ticket creation. Only re-dispatch email if the Ticket
                # was just created here (not on every duplicate call).
                if not hasattr(payment, 'ticket'):
                    seat_numbers = [
                        b.seat.seat_number
                        for b in payment.bookings.select_related('seat')
                    ]
                    ticket = Ticket.objects.create(
                        payment=payment,
                        **_build_ticket_snapshot_defaults(payment, seat_numbers, entity_payment_id),
                    )
                    transaction.on_commit(lambda: _dispatch_ticket_email(ticket.id))
                return 'already_confirmed'
            raise PaymentIntegrityMismatch(
                f"Payment {payment.id} ({razorpay_order_id}) is SUCCESS but its "
                f"Booking rows ({sorted(actual_seat_ids)}) don't match its "
                f"seat_snapshot ({sorted(expected_seat_ids)})."
            )

        if payment.status == PaymentStatus.SUCCESS_UNFULFILLED:
            return 'already_unfulfilled'

        # Correction #3/#1: require BOTH conditions explicitly, fail closed,
        # AND require a genuine, non-empty Razorpay payment ID before any
        # fulfillment happens. A captured/true payment with a missing or
        # empty id is treated as an integrity problem, not a normal
        # "not yet captured" case - Booking rows must never be created
        # without a real Razorpay payment ID to point to.
        if entity_status == 'captured' and entity_captured is True:
            if not entity_payment_id:
                raise PaymentIntegrityMismatch(
                    f"Payment {payment.id} ({razorpay_order_id}) has "
                    f"status='captured' and captured=True but the fetched "
                    f"Razorpay entity has no payment id - refusing to "
                    f"create Booking rows without one."
                )

            seat_ids = list(payment.seat_snapshot)

            # Lock ONLY seats that are BOTH in the snapshot AND belong to
            # payment.theater - a single filter, not two sequential ones.
            seats = list(
                Seat.objects.select_for_update().filter(
                    id__in=seat_ids, theater=payment.theater
                )
            )
            locked_seat_ids = {s.id for s in seats}

            # EXACT-SET verification: every snapshot seat ID must have been
            # found, and nothing extra. A mismatch here means the snapshot
            # references invalid seats or seats from the wrong theater -
            # a data-integrity problem, not an ordinary "seat lost" case,
            # so it's raised rather than silently downgraded.
            if locked_seat_ids != set(seat_ids):
                raise PaymentIntegrityMismatch(
                    f"Payment {payment.id} seat_snapshot {sorted(seat_ids)} does not "
                    f"match locked seats {sorted(locked_seat_ids)} for theater "
                    f"{payment.theater_id}. Seats may be missing, deleted, or "
                    f"belong to a different theater."
                )

            reservations = list(
                SeatReservation.objects.select_for_update().filter(
                    seat_id__in=seat_ids,
                    user=payment.user,
                    expires_at__gt=timezone.now(),
                )
            )
            valid_seat_ids = {r.seat_id for r in reservations}

            # A seat is "lost" (ordinary case) if it's already permanently
            # booked, OR this user no longer holds a valid (non-expired)
            # reservation for it - e.g. the 2-minute window ran out and
            # someone else took it before this payment was confirmed.
            lost_seats = [s for s in seats if s.is_booked or s.id not in valid_seat_ids]

            if lost_seats:
                # Money was captured, but the seats are no longer ours to
                # give. Record this honestly - no Booking rows are
                # created, and this needs a manual refund.
                payment.status = PaymentStatus.SUCCESS_UNFULFILLED
                payment.refund_status = RefundStatus.PENDING
                payment.razorpay_payment_id = entity_payment_id
                payment.save()
                return 'success_unfulfilled_refund_pending'

            # Clean path: create bookings, lock seats permanently, close
            # out the payment, clear reservations, create the Ticket with
            # its immutable snapshot, and schedule the email - all inside
            # this same short transaction (the email itself is only
            # QUEUED here, sent later by Celery, and never before commit).
            seat_numbers = []
            for seat in seats:
                # A real IntegrityError here (e.g. Booking.seat uniqueness
                # violated by a concurrent booking that slipped past the
                # locks above) is NEVER swallowed as a safe no-op - it is
                # re-raised so it surfaces as a genuine, investigable error.
                Booking.objects.create(
                    user=payment.user,
                    seat=seat,
                    movie=payment.theater.movie,
                    theater=payment.theater,
                    payment=payment,
                )
                seat.is_booked = True
                seat.save()
                seat_numbers.append(seat.seat_number)

            SeatReservation.objects.filter(seat_id__in=seat_ids).delete()

            payment.status = PaymentStatus.SUCCESS
            payment.razorpay_payment_id = entity_payment_id
            payment.save()

            ticket = Ticket.objects.create(
                payment=payment,
                **_build_ticket_snapshot_defaults(payment, seat_numbers, entity_payment_id),
            )

            # Queue the email ONLY after this transaction actually commits -
            # never before. transaction.on_commit() defers the callback
            # until Django confirms the outermost atomic block committed,
            # and _dispatch_ticket_email() itself never lets a broker
            # failure make this booking look unsuccessful (correction #2).
            transaction.on_commit(lambda: _dispatch_ticket_email(ticket.id))

            return 'confirmed'

        elif entity_status == 'failed':
            # Do not overwrite a payment that has already succeeded by some
            # other path in the meantime (the lock above already prevents
            # this in practice; this check is a defensive second layer).
            if payment.status not in (PaymentStatus.SUCCESS, PaymentStatus.SUCCESS_UNFULFILLED):
                payment.status = PaymentStatus.FAILED
                payment.razorpay_payment_id = entity_payment_id
                payment.save()
                # Immediate release on EXPLICIT failure - do not wait for
                # the ordinary 2-minute expiry.
                SeatReservation.objects.filter(
                    seat_id__in=payment.seat_snapshot, user=payment.user
                ).delete()
            return 'failed'

        else:
            # e.g. 'authorized', 'created', or 'captured' without a
            # confirmed True 'captured' flag - not yet a terminal,
            # trustworthy success state.
            if payment.status == PaymentStatus.CREATED:
                payment.status = PaymentStatus.ATTEMPTED
                payment.save()
            return 'pending'


def mark_payment_cancelled(razorpay_order_id, user, theater_id):
    """
    Called when the user explicitly cancels/closes the Razorpay Checkout
    widget without completing payment. This is a JS-REPORTED event, not a
    Razorpay-verified one, so it deliberately only ever moves
    CREATED/ATTEMPTED -> CANCELLED - it never touches a payment that might
    have actually gone through by some other channel (webhook arriving
    first, for instance) - the status guard below protects that.

    user and theater_id are REQUIRED and used to scope the lookup itself
    (Payment.objects.get(..., user=user, theater_id=theater_id)), so a
    Payment.DoesNotExist is raised if this order doesn't actually belong
    to the requesting user/theater - a user must never be able to cancel
    or release someone else's reservation by guessing an order id.
    """
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(
            razorpay_order_id=razorpay_order_id, user=user, theater_id=theater_id
        )
        if payment.status in (PaymentStatus.CREATED, PaymentStatus.ATTEMPTED):
            payment.status = PaymentStatus.CANCELLED
            payment.save()
            # Immediate release on explicit cancellation.
            SeatReservation.objects.filter(
                seat_id__in=payment.seat_snapshot, user=payment.user
            ).delete()
        return payment.status