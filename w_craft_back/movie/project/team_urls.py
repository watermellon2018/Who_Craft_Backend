"""User-scoped invitation routes (not tied to a single project URL prefix).

Mounted under ``api/invitations/`` in the root URLConf.
"""

from django.urls import path

from w_craft_back.movie.project.team_views import (
    IncomingInvitationsView,
    InvitationActionView,
    InvitationTokenView,
)

urlpatterns = [
    # Invitations addressed to the current user (shown on "My Projects").
    path("incoming/", IncomingInvitationsView.as_view(), name="invitations-incoming"),
    # Accept / decline a username invitation by id.
    path(
        "<int:invitation_id>/<str:action>/",
        InvitationActionView.as_view(),
        name="invitation-action",
    ),
    # Preview / accept a link invitation by its raw token.
    path("token/<str:token>/", InvitationTokenView.as_view(), name="invitation-token"),
]
