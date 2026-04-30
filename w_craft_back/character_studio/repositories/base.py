class BaseRepository:
    model = None
    pk_field = "pk"

    def create(self, **kwargs):
        return self.model.objects.create(**kwargs)

    def get(self, **filters):
        return self.model.objects.get(**filters)

    def filter(self, **filters):
        return self.model.objects.filter(**filters)

    def all(self):
        return self.model.objects.all()

    def update(self, instance, **kwargs):
        for key, value in kwargs.items():
            setattr(instance, key, value)
        instance.save()
        return instance

    def delete(self, instance):
        instance.delete()

