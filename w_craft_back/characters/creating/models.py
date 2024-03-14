from django.db import models
from django.core.exceptions import ValidationError
from w_craft_back.movie.project.models import Project

def validate_birth_date(value):
    try:
        day, month, year = map(int, value.split('.'))
        if not (1 <= day <= 31 and 1 <= month <= 12 and 1 <= year):
            raise ValueError()
    except (ValueError, TypeError):
        raise ValidationError('Дата должна быть в формате dd.mm.yyyy')


class Character(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE)
    photo = models.ImageField(upload_to='project/hero/promo/')
    first_name = models.CharField(max_length=100, default='')
    last_name = models.CharField(max_length=100, default='')
    middle_name = models.CharField(max_length=100, default='')
    birth_date = models.CharField(max_length=25, default='',
                                  validators=[validate_birth_date])
    birth_place = models.CharField(max_length=100, default='')


class GoalsMotivation(models.Model):
    character = models.ForeignKey(Character, on_delete=models.CASCADE)
    purpose_in_story = models.TextField(default='')
    goal = models.TextField(default='')
    life_philosophy = models.TextField(default='')
    character_development = models.TextField(default='')


class PersonalityTraits(models.Model):
    character = models.ForeignKey(Character, on_delete=models.CASCADE)
    character_type = models.TextField(default='')
    personal_traits = models.TextField(default='')
    strengths_weaknesses = models.TextField(default='')
    complexes = models.TextField(default='')
    inner_conflicts = models.TextField(default='')
    individual_style = models.TextField(default='')


class BiographyRelationships(models.Model):
    character = models.ForeignKey(Character, on_delete=models.CASCADE)
    biography = models.TextField(default='')
    relationships_with_others = models.TextField(default='')
    addit_info = models.TextField(default='')


class ProfessionHobbies(models.Model):
    character = models.ForeignKey(Character, on_delete=models.CASCADE)
    profession = models.CharField(max_length=100, default='')
    hobbies = models.TextField(default='')


class TalentsAbilities(models.Model):
    character = models.ForeignKey(Character, on_delete=models.CASCADE)
    talents = models.TextField(default='')
    intellectual_abilities = models.TextField(default='')
    physical_characteristics = models.TextField(default='')
    external_characteristics = models.TextField(default='')
    speech_patterns = models.TextField(default='')
