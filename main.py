"""Minimal program used to validate automatic cross-platform packaging."""


def main():
    print("Universal Auto Packager demo is running.")
    print("This executable was built automatically by GitHub Actions.")
    try:
        input("Press Enter to exit...")
    except EOFError:
        pass


if __name__ == "__main__":
    main()
