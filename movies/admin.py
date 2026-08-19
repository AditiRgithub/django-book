from django.contrib import admin
from .models import (
    Movie, Theater, Seat, Booking, RecentlyViewed, SeatReservation,
    Payment, WebhookEvent, Ticket,
    MoviePoster, Review, ReviewReport,
)


class MoviePosterInline(admin.TabularInline):
    model = MoviePoster
    extra = 1
    fields = ['image', 'order']


@admin.register(Movie)
class MovieAdmin(admin.ModelAdmin):
    list_display = [
        'name', 'genre', 'language', 'rating', 'release_date', 'cast', 'description',
        'youtube_video_id', 'age_certification', 'duration_minutes',
    ]
    inlines = [MoviePosterInline]


@admin.register(Theater)
class TheaterAdmin(admin.ModelAdmin):
    list_display = ['name', 'movie', 'city', 'screen', 'time', 'ticket_price']


@admin.register(Seat)
class SeatAdmin(admin.ModelAdmin):
    list_display = ['theater', 'seat_number', 'is_booked']


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ['user', 'seat', 'movie', 'theater', 'booked_at', 'payment']


@admin.register(RecentlyViewed)
class RecentlyViewedAdmin(admin.ModelAdmin):
    list_display = ['user', 'movie', 'viewed_at']


@admin.register(SeatReservation)
class SeatReservationAdmin(admin.ModelAdmin):
    list_display = ['seat', 'user', 'reserved_at', 'expires_at']
    list_filter = ['expires_at']


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = [
        'user', 'movie', 'theater', 'amount_rupees',
        'razorpay_order_id', 'razorpay_payment_id',
        'status', 'refund_status', 'created_at',
    ]
    list_filter = ['status', 'refund_status']
    search_fields = ['razorpay_order_id', 'razorpay_payment_id', 'user__username']


@admin.register(WebhookEvent)
class WebhookEventAdmin(admin.ModelAdmin):
    list_display = ['razorpay_event_id', 'event_type', 'status', 'received_at', 'processed_at', 'error_message']
    list_filter = ['status', 'event_type']


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = [
        'booking_reference', 'user', 'payment',
        'email_status', 'email_attempts', 'created_at', 'emailed_at',
    ]
    list_filter = ['email_status']
    search_fields = ['booking_reference', 'user__username']


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ['movie', 'user', 'rating', 'created_at', 'updated_at']
    list_filter = ['rating', 'created_at']
    search_fields = ['movie__name', 'user__username', 'review_text']


@admin.register(ReviewReport)
class ReviewReportAdmin(admin.ModelAdmin):
    list_display = ['review', 'reported_by', 'status', 'created_at']
    list_filter = ['status', 'created_at']
    search_fields = ['review__movie__name', 'reported_by__username', 'reason']
