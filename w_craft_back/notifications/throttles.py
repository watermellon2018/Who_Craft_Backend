from rest_framework.throttling import UserRateThrottle


class NotificationEventThrottle(UserRateThrottle):
    """Bound user-triggered events that can fan out to email delivery."""

    scope = 'notification_event'
    rate = '30/hour'
