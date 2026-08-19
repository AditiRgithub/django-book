"""
Task 2: asynchronous ticket email delivery via Celery.

Queued exclusively from movies/services.py via transaction.on_commit(),
never before the booking/payment transaction has actually committed, and
dispatch failures there are handled without affecting the booking result
(see _dispatch_ticket_email in services.py).
"""
from datetime import timedelta

from celery import shared_task
from celery.utils.log import get_task_logger

from django.conf import settings
from django.core.mail import EmailMessage
from django.db import transaction
from django.utils import timezone

from .models import Ticket
from .ticket_utils import generate_ticket_pdf

logger = get_task_logger(__name__)

# How long a ticket may sit in SENDING before we assume the worker that
# claimed it crashed mid-send, and allow another worker to reclaim it.
# This is the crash-recovery window - without it, a worker dying between
# claiming SENDING and marking SENT/FAILED would block that ticket's email
# forever.
#
# HONEST LIMITATION: this claim mechanism prevents ORDINARY simultaneous
# duplicate Celery workers from both sending (the common case this project
# needs to guard against). It does NOT provide mathematically guaranteed
# exactly-once delivery over SMTP. There is a narrow crash window: if SMTP
# has already accepted the message but the worker crashes before this
# process can persist email_status=SENT, the stale-SENDING recovery below
# will eventually let another worker reclaim and resend it. This is a
# deliberate, documented at-least-once trade-off - true exactly-once email
# delivery isn't achievable with plain SMTP without a distributed
# transactional outbox, which is out of scope here.
STALE_SENDING_TIMEOUT_SECONDS = 600  # 10 minutes


def _claim_ticket_for_sending(ticket_id, force=False):
    """
    Atomically claims this ticket for sending. Returns the claimed Ticket
    instance if THIS call won the claim, or None if another concurrent
    delivery already claimed it (genuinely in-flight SENDING) or it's
    already SENT (unless force=True). The row lock is held ONLY for this
    short claim transaction - it is released immediately, well before any
    SMTP call happens, so two simultaneous send_ticket_email_task(ticket.id)
    calls can never both win the claim and both send the email.
    """
    with transaction.atomic():
        ticket = Ticket.objects.select_for_update().get(id=ticket_id)

        if ticket.email_status == Ticket.EmailStatus.SENT and not force:
            return None

        if ticket.email_status == Ticket.EmailStatus.SENDING:
            stale_cutoff = timezone.now() - timedelta(seconds=STALE_SENDING_TIMEOUT_SECONDS)
            still_fresh = (
                ticket.email_status_updated_at is not None
                and ticket.email_status_updated_at > stale_cutoff
            )
            if still_fresh:
                # Genuinely being sent right now by another worker - do
                # not send a second email.
                return None
            # else: stale SENDING left behind by a crashed worker - safe
            # to reclaim below.

        ticket.email_status = Ticket.EmailStatus.SENDING
        ticket.email_status_updated_at = timezone.now()
        ticket.email_attempts += 1
        ticket.save(update_fields=['email_status', 'email_status_updated_at', 'email_attempts'])
        return ticket


@shared_task(
    bind=True,
    max_retries=5,
    autoretry_for=(Exception,),
    retry_backoff=True,      # exponential backoff between retries
    retry_backoff_max=600,   # cap at 10 minutes between retries
    retry_jitter=True,       # jitter to avoid thundering-herd retries
)
def send_ticket_email_task(self, ticket_id, force=False):
    """
    Concurrency-safe: claims the ticket via a short atomic SELECT FOR
    UPDATE before doing anything else (see _claim_ticket_for_sending). No
    database transaction/row lock is held while SMTP is sending - the
    claim transaction commits and releases its lock immediately, and the
    actual email send happens afterward, outside any transaction.
    """
    claimed = _claim_ticket_for_sending(ticket_id, force=force)
    if claimed is None:
        return 'already_claimed_sending_or_sent'

    ticket = Ticket.objects.select_related(
        'payment', 'payment__theater', 'payment__movie', 'user'
    ).get(id=ticket_id)

    try:
        pdf_buffer = generate_ticket_pdf(ticket)

        subject = f'Your BookMySeat Ticket - {ticket.snapshot_movie_name}'
        body = (
            f'Hi {ticket.user.username},\n\n'
            f'Your booking is confirmed!\n\n'
            f'Movie: {ticket.snapshot_movie_name}\n'
            f'Theater: {ticket.snapshot_theater_name}\n'
            f'Screen: {ticket.snapshot_screen}\n'
            f'Show Time: {timezone.localtime(ticket.snapshot_show_time)}\n'
            f'Seats: {", ".join(ticket.snapshot_seat_numbers)}\n'
            f'Booking Reference: {ticket.booking_reference}\n\n'
            f'Your e-ticket is attached as a PDF. Please carry it (digital '
            f'or printed) along with a valid ID to the theater.\n\n'
            f'Thank you for booking with BookMySeat!'
        )

        email = EmailMessage(
            subject=subject,
            body=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[ticket.user.email],
        )
        email.attach(f'{ticket.booking_reference}.pdf', pdf_buffer.read(), 'application/pdf')
        # No DB transaction/row lock is open here - the claim above already
        # committed and released its lock before this call.
        email.send(fail_silently=False)

    except Exception as exc:
        Ticket.objects.filter(id=ticket_id).update(
            email_status=Ticket.EmailStatus.FAILED,
            email_status_updated_at=timezone.now(),
            last_email_error=str(exc),
        )
        logger.exception('send_ticket_email_task failed for ticket %s', ticket_id)
        # autoretry_for handles the actual retry scheduling; re-raise so
        # Celery registers this attempt as failed and schedules the next
        # one. The FAILED status above means a subsequent retry's claim
        # will succeed (status != SENT/fresh-SENDING).
        raise

    Ticket.objects.filter(id=ticket_id).update(
        email_status=Ticket.EmailStatus.SENT,
        email_status_updated_at=timezone.now(),
        emailed_at=timezone.now(),
        last_email_error=None,
    )
    return 'sent'