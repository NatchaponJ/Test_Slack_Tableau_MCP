from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import os
from dotenv import load_dotenv
load_dotenv()

TABLEAU_TOKEN_NAME    = os.getenv("TABLEAU_TOKEN_NAME")
TABLEAU_TOKEN_VALUE   = os.getenv("TABLEAU_TOKEN_VALUE")
TABLEAU_SITE_ID       = os.getenv("TABLEAU_SITE_ID")
TABLEAU_SERVER_URL    = os.getenv("TABLEAU_SERVER_URL")


server_params = StdioServerParameters(
    command="npx",
    args=["-y", "@tableau/mcp-server@latest"],
    env={
        "SERVER": TABLEAU_SERVER_URL,
        "SITE_NAME": TABLEAU_SITE_ID,
        "PAT_NAME": TABLEAU_TOKEN_NAME,
        "PAT_VALUE": TABLEAU_TOKEN_VALUE,
    },
)

async def get_mcp_session():
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return session
