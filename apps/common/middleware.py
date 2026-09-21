"""
Response compression for the API.

JSON lists compress to roughly a quarter of their size, which matters on the
clinic's mobile connections more than anything the server does.

Authentication responses are left uncompressed. They carry tokens, and a
compressed response whose size an attacker can observe while also injecting
text into it is the BREACH attack; nothing is lost by skipping the one small
response per login.
"""

from django.middleware.gzip import GZipMiddleware

UNCOMPRESSED_PREFIXES = ("/api/auth/",)


class ApiGZipMiddleware(GZipMiddleware):
    def process_response(self, request, response):
        if request.path.startswith(UNCOMPRESSED_PREFIXES):
            return response
        return super().process_response(request, response)
