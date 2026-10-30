import os
from dotenv import load_dotenv
from crewai import Agent, Task, Crew, Process, LLM

load_dotenv()

api_key = os.getenv("groqkey")

if not api_key:
    raise ValueError("groqkey is missing from your .env file")

# Connect CrewAI to Groq's OpenAI-compatible API
llm = LLM(
    model="openai/openai/gpt-oss-120b",
    custom_openai=True,
    base_url="https://api.groq.com/openai/v1",
    api_key=api_key,
    temperature=0.2,
)

# Create a test agent
email_agent = Agent(
    role="Professional Email Assistant",
    goal="Write clear, concise, and professional email replies.",
    backstory="You are an email assistant that writes polite and accurate replies.",
    llm=llm,
    verbose=True,
)

# Give the agent a task
email_task = Task(
    description=(
        "Write a short professional email reply acknowledging receipt "
        "of a project update request. Do not invent deadlines or commitments."
    ),
    expected_output="A short, professional email reply.",
    agent=email_agent,
)

# Run the CrewAI workflow
crew = Crew(
    agents=[email_agent],
    tasks=[email_task],
    process=Process.sequential,
    verbose=True,
)

result = crew.kickoff()

print("\n" + "=" * 50)
print("SUCCESS: CrewAI connected to Groq!")
print("=" * 50)
print(result)
