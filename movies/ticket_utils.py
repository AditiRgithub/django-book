"""
Task 2: centralized ticket PDF + QR generation.

Used by BOTH the async email task (movies/tasks.py) and the synchronous
ticket-download view (movies/views.py), so the PDF layout logic lives in
exactly one place - no duplicated generation code between the two paths.

Reads ONLY the immutable snapshot fields stored on Ticket at creation time
(correction #4) - never live Movie/Theater rows - so a ticket downloaded
today looks identical to one downloaded a year from now, even if the
theater's name/screen/showtime was later edited in admin.

Generates entirely in memory via BytesIO; never writes a temp file to disk.
"""
import io

import qrcode
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from django.conf import settings
from django.utils import timezone


def build_verification_url(ticket):
    """
    Builds the QR-code target URL using SITE_BASE_URL, since the Celery
    worker has no HTTP request object to derive a host from. Contains
    only the random verification token - no secrets, no payment details.
    """
    base = settings.SITE_BASE_URL.rstrip('/')
    return f'{base}/tickets/verify/{ticket.verification_token}/'


def _generate_qr_image_reader(data):
    qr = qrcode.QRCode(box_size=6, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color='black', back_color='white')
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return ImageReader(buf)


def generate_ticket_pdf(ticket):
    """
    Returns a BytesIO containing the rendered PDF for the given Ticket.
    Pulls all booking-time details from Ticket's own immutable snapshot
    fields - never from Payment.theater/Payment.movie (which are live,
    mutable rows) - so calling this twice for the same Ticket, even years
    apart, always produces the same content regardless of later admin edits.
    """
    seat_numbers = ticket.snapshot_seat_numbers or []

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    y = height - 30 * mm
    c.setFont('Helvetica-Bold', 20)
    c.drawString(20 * mm, y, 'BookMySeat')
    y -= 8 * mm
    c.setFont('Helvetica', 10)
    c.drawString(20 * mm, y, 'E-Ticket')

    y -= 15 * mm
    c.setFont('Helvetica-Bold', 15)
    c.drawString(20 * mm, y, ticket.snapshot_movie_name)

    details = [
        ('Booking Reference', ticket.booking_reference),
        ('Theater', ticket.snapshot_theater_name),
        ('Screen', ticket.snapshot_screen or 'Screen 1'),
        ('Show Date/Time', timezone.localtime(ticket.snapshot_show_time).strftime('%d %b %Y, %I:%M %p')),
        ('Seats', ', '.join(seat_numbers) if seat_numbers else '-'),
        ('Number of Tickets', str(len(seat_numbers))),
        ('Amount Paid', f'Rs. {ticket.snapshot_amount_rupees}'),
        ('Payment Reference', ticket.snapshot_payment_reference or '-'),
        ('Booking Date', timezone.localtime(ticket.created_at).strftime('%d %b %Y, %I:%M %p')),
    ]

    y -= 12 * mm
    c.setFont('Helvetica', 11)
    for label, value in details:
        c.drawString(20 * mm, y, f'{label}: {value}')
        y -= 7 * mm

    # QR code, top-right of the page.
    qr_reader = _generate_qr_image_reader(build_verification_url(ticket))
    c.drawImage(qr_reader, width - 60 * mm, height - 90 * mm, 40 * mm, 40 * mm)

    y -= 10 * mm
    c.setFont('Helvetica-Oblique', 8)
    c.drawString(20 * mm, y, 'Scan the QR code above to verify this ticket.')
    y -= 5 * mm
    c.drawString(20 * mm, y, 'This ticket is valid only for the movie, theater, and showtime listed above.')

    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer