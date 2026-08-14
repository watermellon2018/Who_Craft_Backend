from django.core.exceptions import ValidationError


def validate_birth_date(value: object) -> None:
    """Validate the historical Character.birth_date migration field."""

    try:
        day, month, year = map(int, str(value).split("."))
        if not (1 <= day <= 31 and 1 <= month <= 12 and 1 <= year):
            raise ValueError
    except (ValueError, TypeError):
        raise ValidationError(
            "Дата должна быть в формате dd.mm.yyyy"
        ) from None
