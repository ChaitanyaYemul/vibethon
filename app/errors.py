class ApiError(Exception):
    def __init__(self, status, code, message, **details):
        self.status = status
        self.code = code
        self.message = message
        self.details = details
        super().__init__(message)
