import os
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

api_key = os.getenv("groqkey")

if not api_key:
    raise ValueError("groqkey is missing from .env")

client = OpenAI(
    api_key=api_key,
    base_url="https://api.groq.com/openai/v1",
)

response = client.chat.completions.create(
    model="openai/gpt-oss-120b",
    messages=[
        {
            "role": "user",
            "content": (
                "Write a short, professional email reply "
                "acknowledging receipt of a project update request."
            ),
        }
    ],
    temperature=0.2,
)

print("\nGroq connection successful!")
print("Model:", response.model)
print("\nGenerated reply:\n")
print(response.choices[0].message.content)
