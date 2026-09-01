from django.conf import settings
from django.core.mail import send_mail


def send_notification_email(*, recipient_email: str, title: str, message: str) -> None:
    """Deliver one notification through Django's configured email backend."""
    send_mail(
        subject=title,
        message=message,
        from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', None),
        recipient_list=[recipient_email],
        fail_silently=False,
    )
