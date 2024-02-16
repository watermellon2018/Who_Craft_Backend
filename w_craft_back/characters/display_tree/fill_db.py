# import django
# django.setup()
# from w_craft_back.characters.display_tree.models import MenuFolder, ItemFolder
#
# main_characters_folder = MenuFolder.objects.create(name="Main characters", is_folder=True)
# secondary_characters_folder = MenuFolder.objects.create(name="Secondary characters", is_folder=True)
#
# # Создание вложенных папок
# small_characters_folder = MenuFolder.objects.create(name="Little", is_folder=True, parent=main_characters_folder)
# teen_characters_folder = MenuFolder.objects.create(name="Teenagers", is_folder=True, parent=main_characters_folder)
# young_characters_folder = MenuFolder.objects.create(name="Young", is_folder=True, parent=main_characters_folder)
# mature_characters_folder = MenuFolder.objects.create(name="Adult", is_folder=True, parent=main_characters_folder)
#
# # Создание персонажей
# zhena_character = ItemFolder.objects.create(name="Zhenya", is_folder=False)
# kolya_character = ItemFolder.objects.create(name="Koly", is_folder=False)
#
# # Связывание персонажей с соответствующими папками
# zhena_character.parent = small_characters_folder
# zhena_character.save()
#
# kolya_character.parent = small_characters_folder
# kolya_character.save()
#
# # Второстепенные персонажи
# secondary_character1 = ItemFolder.objects.create(name="Secondary characters 1", is_folder=False)
# secondary_character2 = ItemFolder.objects.create(name="Secondary characters 2", is_folder=False)
# secondary_character3 = ItemFolder.objects.create(name="Secondary characters 3", is_folder=False)
#
# # Связывание второстепенных персонажей с главной папкой
# secondary_character1.parent = secondary_characters_folder
# secondary_character1.save()
#
# secondary_character2.parent = secondary_characters_folder
# secondary_character2.save()
#
# secondary_character3.parent = secondary_characters_folder
# secondary_character3.save()
