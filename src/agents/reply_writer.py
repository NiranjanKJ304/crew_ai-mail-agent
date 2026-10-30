from crewai import Agent

def create_reply_writer(llm, config):
    return Agent(
        llm=llm,
        role=config["role"],
        goal=config["goal"],
        backstory=config["backstory"],
        verbose=True,
    )
