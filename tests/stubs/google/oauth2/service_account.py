class Credentials:
    @classmethod
    def from_service_account_info(cls, info, scopes=None):
        return cls()

    @classmethod
    def from_service_account_file(cls, path, scopes=None):
        return cls()
