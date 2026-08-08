# Local authentication

The local single-user setup does not expose a “forgot password” workflow.
Reset the account password from the backend directory with Django's standard
administration command:

```shell
python manage.py changepassword <username>
```

The command prompts for the new password and applies the configured Django
password validators.
