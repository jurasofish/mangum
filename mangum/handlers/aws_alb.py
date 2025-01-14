import base64
import urllib.parse
from typing import Any, Dict, Generator, List, Tuple

from .abstract_handler import AbstractHandler
from .. import Response, Request


def all_casings(input_string: str) -> Generator:
    """
    Permute all casings of a given string.
    A pretty algoritm, via @Amber
    http://stackoverflow.com/questions/6792803/finding-all-possible-case-permutations-in-python
    """
    if not input_string:
        yield ""
    else:
        first = input_string[:1]
        if first.lower() == first.upper():
            for sub_casing in all_casings(input_string[1:]):
                yield first + sub_casing
        else:
            for sub_casing in all_casings(input_string[1:]):
                yield first.lower() + sub_casing
                yield first.upper() + sub_casing


class AwsAlb(AbstractHandler):
    """
    Handles AWS Elastic Load Balancer, really Application Load Balancer events
    transforming them into ASGI Scope and handling responses

    See: https://docs.aws.amazon.com/lambda/latest/dg/services-alb.html
    """

    TYPE = "AWS_ALB"

    def encode_query_string(self) -> bytes:
        """
        Encodes the queryStringParameters.
        The parameters must be decoded, and then encoded again to prevent double
        encoding.

        https://docs.aws.amazon.com/elasticloadbalancing/latest/application/lambda-functions.html  # noqa: E501
        "If the query parameters are URL-encoded, the load balancer does not decode
        them. You must decode them in your Lambda function."

        Issue: https://github.com/jordaneremieff/mangum/issues/178
        """

        params = self.trigger_event.get("multiValueQueryStringParameters")
        if not params:
            params = self.trigger_event.get("queryStringParameters")
        if not params:
            return b""  # No query parameters, exit early with an empty byte string.

        # Loop through the query parameters, unquote each key and value and append the
        # pair as a tuple to the query list. If value is a list or a tuple, loop
        # through the nested struture and unqote.
        query = []
        for key, value in params.items():
            if isinstance(value, (tuple, list)):
                for v in value:
                    query.append(
                        (urllib.parse.unquote_plus(key), urllib.parse.unquote_plus(v))
                    )
            else:
                query.append(
                    (urllib.parse.unquote_plus(key), urllib.parse.unquote_plus(value))
                )

        return urllib.parse.urlencode(query).encode()

    @property
    def request(self) -> Request:
        event = self.trigger_event

        headers = {}
        if event.get("headers"):
            headers = {k.lower(): v for k, v in event.get("headers", {}).items()}

        source_ip = headers.get("x-forwarded-for", "")
        path = event["path"]
        http_method = event["httpMethod"]
        query_string = self.encode_query_string()

        server_name = headers.get("host", "mangum")
        if ":" not in server_name:
            server_port = headers.get("x-forwarded-port", 80)
        else:
            server_name, server_port = server_name.split(":")  # pragma: no cover
        server = (server_name, int(server_port))
        client = (source_ip, 0)

        if not path:
            path = "/"

        return Request(
            method=http_method,
            headers=[[k.encode(), v.encode()] for k, v in headers.items()],
            path=urllib.parse.unquote(path),
            scheme=headers.get("x-forwarded-proto", "https"),
            query_string=query_string,
            server=server,
            client=client,
            trigger_event=self.trigger_event,
            trigger_context=self.trigger_context,
            event_type=self.TYPE,
        )

    @property
    def body(self) -> bytes:
        body = self.trigger_event.get("body", b"") or b""

        if self.trigger_event.get("isBase64Encoded", False):
            return base64.b64decode(body)
        if not isinstance(body, bytes):
            body = body.encode()

        return body

    def handle_headers(
        self,
        response_headers: List[List[bytes]],
    ) -> Tuple[Dict[str, str], Dict[str, List[str]]]:
        headers, multi_value_headers = self._handle_multi_value_headers(
            response_headers
        )
        if "multiValueHeaders" not in self.trigger_event:
            # If there are multiple occurrences of headers, create case-mutated
            # variations: https://github.com/logandk/serverless-wsgi/issues/11
            for key, values in multi_value_headers.items():
                if len(values) > 1:
                    for value, cased_key in zip(values, all_casings(key)):
                        headers[cased_key] = value

            multi_value_headers = {}

        return headers, multi_value_headers

    def transform_response(self, response: Response) -> Dict[str, Any]:
        headers, multi_value_headers = self.handle_headers(response.headers)

        body, is_base64_encoded = self._handle_base64_response_body(
            response.body, headers
        )

        return {
            "statusCode": response.status,
            "headers": headers,
            "multiValueHeaders": multi_value_headers,
            "body": body,
            "isBase64Encoded": is_base64_encoded,
        }
