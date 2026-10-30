from src.email_service.gmail_auth import get_gmail_service


def main():
    print("Connecting to Gmail...")

    service = get_gmail_service()

    profile = (
        service.users()
        .getProfile(userId="me")
        .execute()
    )

    authenticated_email = profile["emailAddress"]

    print("\nGmail authentication successful!")
    print(f"Authenticated account: {authenticated_email}")
    print(f"Total messages: {profile.get('messagesTotal', 0)}")


if __name__ == "__main__":
    main()
    