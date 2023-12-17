def get_promt_age(min_value, max_value) -> str:

    if min_value is None and max_value is None:
        return ''

    if min_value is not None and max_value is not None:
        promt = 'Age range from {} to {}. '.format(min_value, max_value)
        return promt

    promt = ''
    if min_value is None:
        promt = 'The character must be at least {} years old. '.format(min_value)

    elif max_value is None:
        promt = 'The character must be no more than {} years old. '.format(max_value)

    return promt
