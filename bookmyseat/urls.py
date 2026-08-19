from django.contrib import admin
from django.urls import path, include, re_path
from django.conf import settings
from django.views.static import serve

from movies import views as movies_views


urlpatterns = [
    path('admin/', admin.site.urls),
    path('users/', include('users.urls')),
    path('', include('users.urls')),
    path('movies/', include('movies.urls')),

    # QR verification URL used by ticket generation
    path(
        'tickets/verify/<uuid:token>/',
        movies_views.verify_ticket,
        name='verify_ticket'
    ),

    # Serve movie poster/media files on Vercel
    re_path(
        r'^media/(?P<path>.*)$',
        serve,
        {'document_root': settings.MEDIA_ROOT}
    ),
]