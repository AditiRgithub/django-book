from django.urls import path
from . import views

urlpatterns = [
    path('', views.movie_list, name='movie_list'),
    path('<int:movie_id>/theaters', views.theater_list, name='theater_list'),
    path('theater/<int:theater_id>/seats/book/', views.book_seats, name='book_seats'),
    path('theater/<int:theater_id>/reservation/confirm/', views.reservation_confirmation, name='reservation_confirmation'),
    path('theater/<int:theater_id>/payment/placeholder/', views.payment_placeholder, name='payment_placeholder'),
    path('theater/<int:theater_id>/payment/callback/', views.payment_callback, name='payment_callback'),
    path('theater/<int:theater_id>/payment/cancel/', views.payment_cancel, name='payment_cancel'),
    path('payment/webhook/', views.razorpay_webhook, name='razorpay_webhook'),
    path('tickets/<int:ticket_id>/download/', views.download_ticket, name='download_ticket'),
    path('admin-dashboard/', views.admin_dashboard, name='admin_dashboard'),
    path('admin-dashboard/export/', views.export_dashboard_csv, name='export_dashboard_csv'),
    path('<int:movie_id>/details/', views.movie_detail, name='movie_detail'),
    path('<int:movie_id>/reviews/create/', views.create_review, name='create_review'),
    path('reviews/<int:review_id>/edit/', views.edit_review, name='edit_review'),
    path('reviews/<int:review_id>/report/', views.report_review, name='report_review'),
]