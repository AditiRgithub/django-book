from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

from movies import views as movies_views


urlpatterns = [
    path('admin/', admin.site.urls),
    path('users/', include('users.urls')),
    path('', include('users.urls')),
    path('movies/', include('movies.urls')),

    # QR verification URL used by Task 2 ticket generation
    path(
        'tickets/verify/<uuid:token>/',
        movies_views.verify_ticket,
        name='verify_ticket'
    ),
]


if settings.DEBUG:
    urlpatterns += static(
        settings.MEDIA_URL,
        document_root=settings.MEDIA_ROOT
    )