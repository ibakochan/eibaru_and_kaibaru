from django.apps import AppConfig


class KaibaruConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'kaibaru'

    def ready(self):
        import kaibaru.signals
        from kaibaru.stripe_failure import install_stripe_failure_client

        install_stripe_failure_client()