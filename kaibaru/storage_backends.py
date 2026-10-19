from storages.backends.gcloud import GoogleCloudStorage

class PrivateMediaStorage(GoogleCloudStorage):
    bucket_name = "kaibaru"
    file_overwrite = False      
    querystring_auth = True


class ExamplePageImageStorage(GoogleCloudStorage):
    """Shared layout photos in the sitepicsforkaibaru bucket.

    Layout JSON stores the public URL directly. These objects are not owned
    by a SlateImage row, and delete() does nothing so a model using this
    storage cannot remove them from the bucket.
    """

    bucket_name = "sitepicsforkaibaru"
    querystring_auth = False
    default_acl = None

    def __init__(self, **settings):
        super().__init__(**settings)
        # GS_FILE_OVERWRITE is False project-wide. Re-uploads of a template
        # photo must replace the same object name.
        self.file_overwrite = True

    def delete(self, name):
        return

    def url(self, name):
        name = str(name).lstrip("/")
        return f"https://storage.googleapis.com/{self.bucket_name}/{name}"