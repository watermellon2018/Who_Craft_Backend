from w_craft_back.movie.properties.models import *
from w_craft_back.movie.project.models import *
from w_craft_back.auth.models import *
from w_craft_back.character_studio.models import *
from w_craft_back.character_studio.tree_models import *
from w_craft_back.credits.models import *
from w_craft_back.profile.models import *
from w_craft_back.subscriptions.models import *
from w_craft_back.notifications.models import *
# Dashboard models depend on StudioCharacter — import last.
from w_craft_back.movie.project.dashboard_models import *
from w_craft_back.movie.project.comment_models import *
# Reference Library models depend on ProjectAsset, Scene and StudioCharacter.
from w_craft_back.movie.reference_library.models import *
# Storyboard models depend on the project dashboard and Reference Library.
from w_craft_back.movie.storyboard.models import *
# Music models depend on Project + dashboard MusicTrack.
from w_craft_back.movie.music.models import *
# Sound Effects is a separate reusable-audio domain with immutable versions.
from w_craft_back.movie.sound_effects.models import *
# Team-collaboration models depend on dashboard role enums + Project.
from w_craft_back.movie.project.team_models import *
# Poster models depend on Project + ProjectAsset (defined above).
from w_craft_back.movie.poster.models import *
