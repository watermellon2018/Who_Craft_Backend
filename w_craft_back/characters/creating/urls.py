from w_craft_back.characters.creating.view import create_hero, select_all,\
    select_hero_by_id, delete_hero_by_id
from w_craft_back.characters.creating.view_for_update import update_personal_info,\
    update_motivate_info, update_inside_data_hero, update_competition_data_hero,\
    update_identity_data_hero, update_psyho_data_hero, update_development_data_hero,\
    update_addit_data_hero, update_bio_data_hero, update_relationship_data_hero, \
    update_image_hero
from django.urls import path


urlpatterns = [
    path('select_by_id/', select_hero_by_id, name='select_hero_by_id'),
    path('delete_by_id/', delete_hero_by_id, name='delete_hero_by_id'),
    path('create/', create_hero, name='create_hero'),
    path('select/', select_all, name='select_all'),
    path('update_personal_info/', update_personal_info, name='update_personal_info'),
    path('update_motivate_info/', update_motivate_info, name='update_motivate_info'),
    path('update_inside_info/', update_inside_data_hero, name='update_inside_data_hero'),
    path('update_competition_info/', update_competition_data_hero, name='update_competition_data_hero'),
    path('update_identity_info/', update_identity_data_hero, name='update_identity_data_hero'),
    path('update_psyho_info/', update_psyho_data_hero, name='update_psyho_data_hero'),
    path('update_development_info/', update_development_data_hero, name='update_development_data_hero'),
    path('update_additional_info/', update_addit_data_hero, name='update_addit_data_hero'),
    path('update_bio_info/', update_bio_data_hero, name='update_bio_data_hero'),
    path('update_relationship_info/', update_relationship_data_hero, name='update_relationship_data_hero'),
    path('update_image_hero/', update_image_hero, name='update_image_hero'),
]
