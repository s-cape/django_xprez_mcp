from django.http import JsonResponse


class ClientError(Exception):
    """An unknown or invalid client: shown on a page, never redirected."""

    def __init__(self, message, code="invalid_client"):
        super().__init__(message)
        self.code = code


class OAuthError(Exception):
    """An error of the token, registration or revocation endpoint."""

    def __init__(self, error, description=None, status=400):
        super().__init__(description or error)
        self.error = error
        self.description = description
        self.status = status

    def response(self):
        payload = {"error": self.error}
        if self.description:
            payload["error_description"] = self.description
        response = JsonResponse(payload, status=self.status)
        response["Cache-Control"] = "no-store"
        return response
