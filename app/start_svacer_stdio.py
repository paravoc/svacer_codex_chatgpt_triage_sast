"""Optional stdio launcher, independent of the current working directory."""
from triage_connector.server import main

if __name__ == "__main__":
    main(transport="stdio")
