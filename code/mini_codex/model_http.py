"""Trusted controller-side HTTP child. Never mounted with credentials into a tool worker.

Parent enforces the whole request deadline by killing/reaping this process group.
The private stdin envelope may contain a key; neither headers nor that envelope
are written to traces. This isolated-mode script imports only the standard library.
"""
import http.client
import json
import socket
import ssl
import sys


def request(packet):
    scheme, host, port = packet["endpoint"]
    api_path = packet.get("api_path", "/v1/responses")
    supported = {
        ("https", "api.openai.com", 443): "/v1/responses",
        ("https", "api.deepseek.com", 443): "/responses",
    }
    loopback = scheme == "http" and host == "127.0.0.1" and api_path in supported.values()
    if not loopback and ((scheme, host, port) not in supported or
                         supported[(scheme, host, port)] != api_path):
        return {"request_id": None, "response_status": "failed", "output_items": [],
                "provider_error": "invalid_endpoint"}
    factory = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
    conn = factory(host, port, timeout=packet["timeout"])
    result = {"request_id": None, "response_status": "failed", "output_items": []}
    try:
        conn.request("POST", api_path, body=json.dumps(packet["payload"]).encode(),
                     headers={"Authorization": f"Bearer {packet['key']}",
                              "Content-Type": "application/json"})
        response = conn.getresponse()
        result["request_id"] = response.getheader("x-request-id")
        if response.status != 200:
            # Never echo the error body: it may contain submitted credentials or data.
            result["provider_error"] = f"provider_http_{response.status}"
            return result
        raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            result["provider_error"] = "response_too_large"
            return result
        decoded = json.loads(raw)
        if not isinstance(decoded.get("output"), list):
            raise ValueError("invalid output")
        return {"request_id": result["request_id"], "response_id": decoded.get("id"),
                "response_status": decoded.get("status", "unknown"),
                "output_items": decoded["output"],
                "usage": decoded.get("usage") if isinstance(decoded.get("usage"), dict) else None,
                "usage_presence": ("missing" if "usage" not in decoded else
                                   "null" if decoded["usage"] is None else
                                   "object" if isinstance(decoded["usage"], dict) else "invalid")}
    except (TimeoutError, socket.timeout):
        result["response_status"] = "unknown"
        result["provider_error"] = "model_timeout"
        return result
    except OSError:
        result["provider_error"] = "provider_unreachable"
        return result
    except (ValueError, http.client.HTTPException):
        result["provider_error"] = "model_response_format"
        return result
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        print(json.dumps(request(json.load(sys.stdin))))
    except Exception:
        # No traceback containing packet locals, URL/header data or secret text.
        print(json.dumps({"request_id": None, "response_status": "failed", "output_items": [],
                          "provider_error": "model_child_failure"}))
