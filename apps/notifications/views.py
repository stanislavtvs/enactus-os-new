from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_GET


@login_required
@require_GET
def notifications_page(request):
    return render(request, "notifications/index.html", {
        "notification_api_url": reverse("api-1.0.0:notification_list"),
    })
