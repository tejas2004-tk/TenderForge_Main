from groq import APIConnectionError, APIStatusError, APITimeoutError, Groq, RateLimitError


class GroqServiceError(Exception):
    def __init__(self, message, status_code=502):
        super().__init__(message)
        self.status_code = status_code


def test_groq_connection(api_key, model):
    if not api_key:
        raise GroqServiceError("Groq is not configured. Set GROQ_API_KEY in backend/.env.", 503)

    client = Groq(api_key=api_key, timeout=15.0, max_retries=0)
    try:
        result = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Reply with: TenderForge connection successful."}],
            max_tokens=30,
        )
        answer = result.choices[0].message.content
        return answer.strip() if answer else "Groq responded successfully with an empty message."
    except RateLimitError as error:
        raise GroqServiceError("Groq rate limit reached. Please try again later.", 429) from error
    except APITimeoutError as error:
        raise GroqServiceError("Groq request timed out. Please try again.", 504) from error
    except APIConnectionError as error:
        raise GroqServiceError("Could not connect to Groq. Check your network and try again.", 502) from error
    except APIStatusError as error:
        raise GroqServiceError("Groq rejected the request. Check your model name and API key.", 502) from error
    except Exception as error:
        raise GroqServiceError("Groq request failed. Check the backend configuration and try again.", 502) from error
