from crewai import Agent

def create_email_analyzer(llm, config):
    return Agent(
        llm=llm,
        role=config["role"],
        goal=config["goal"],
        backstory=config["backstory"],
        verbose=True,
    )
