"""Test manifest server for serving agent manifests during testing."""

import os
from pathlib import Path
from typing import Annotated, Any

import yaml
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile

# Load fixture files directory
FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _fixture_path(filename: str) -> Path:
    """The fixture a request names: a bare ``*.yaml`` name inside FIXTURES_DIR.

    Every endpoint takes ``filename`` from the request and the server listens
    on all interfaces, so a name such as ``../../.env`` must never resolve to a
    file outside the fixtures directory, for reading or for writing.
    """
    # realpath + startswith is the containment check the code scanner models.
    base = os.path.realpath(FIXTURES_DIR)
    path = os.path.realpath(os.path.join(base, filename))  # noqa: PTH118
    if (
        not path.startswith(base + os.sep)
        or Path(path).name != filename
        or not filename.endswith(".yaml")
    ):
        raise ValueError(f"Not a manifest fixture name: {filename}")
    return Path(path)


async def load_manifest_from_file(filename: str) -> dict[str, Any]:
    """Load manifest from a YAML file in the fixtures directory."""
    file_path = _fixture_path(filename)

    if not file_path.exists():
        raise FileNotFoundError(f"Manifest file not found: {filename}")

    if not file_path.is_file():
        raise ValueError(f"Path is not a file: {filename}")

    with file_path.open() as f:
        return yaml.safe_load(f)


def list_available_manifests() -> list:
    """List all available manifest files in fixtures directory."""
    if not FIXTURES_DIR.exists():
        return []
    return [f.name for f in FIXTURES_DIR.glob("*.yaml") if f.is_file()]


# ── Capability helpers ──────────────────────────────────────────────────────────


def _get_mcp_tools(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract MCP tools from the manifest capabilities section."""
    return manifest.get("capabilities", {}).get("tools", [])


def _get_mcp_resources(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract MCP resources from the manifest capabilities section."""
    return manifest.get("capabilities", {}).get("resources", [])


def _get_mcp_prompts(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract MCP prompts from the manifest capabilities section."""
    return manifest.get("capabilities", {}).get("prompts", [])


def _get_a2a_skills(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract A2A skills from the manifest capabilities section."""
    return manifest.get("capabilities", {}).get("skills", [])


def _build_agent_card(filename: str, manifest: dict[str, Any]) -> dict[str, Any]:
    """Build an A2A agent card from a manifest."""
    identity = manifest.get("identity", {})
    auth_schemes = [
        s["type"] for s in manifest.get("security", {}).get("auth_strategies", [])
    ]
    skills = _get_a2a_skills(manifest)
    endpoint = (
        manifest.get("protocol", {}).get("transport", {}).get("endpoint")
        or f"http://localhost:9000/agents/{filename}"
    )

    return {
        "schemaVersion": "1.0",
        "name": identity.get("name", "TestAgent"),
        "description": identity.get("description", ""),
        "url": endpoint,
        "provider": identity.get("provider", "test"),
        "tags": identity.get("tags", []),
        "authSchemes": auth_schemes,
        "skills": skills,
    }


# Initialize FastAPI app
app = FastAPI(
    title="Test Manifest Server",
    description="Serves agent manifests for testing",
    version="1.0.0",
)


@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "service": "Test Manifest Server",
        "available_manifests": list_available_manifests(),
        "endpoints": {
            "manifest": "/manifest?filename=<filename.yaml>",
            "mcp_rpc": "/agents/<filename.yaml>",
            "a2a_agent_card": "/agents/<filename.yaml>/.well-known/agent.json",
            "health": "/health",
        },
    }


@app.get("/manifest")
async def get_manifest(
    filename: str = Query(..., description="Manifest YAML filename"),
):
    """
    Get agent manifest by filename.

    Query Parameters:
        - filename: Manifest YAML filename (e.g., a2a_logistics.yaml)

    Returns:
        Agent manifest as YAML/JSON
    """
    try:
        return await load_manifest_from_file(filename)
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(
            status_code=404,
            detail={"error": str(e), "available_manifests": list_available_manifests()},
        ) from e


@app.post("/agents/{filename}")
async def mcp_rpc(filename: str, request: Request):
    """
    Mock MCP JSON-RPC endpoint for testing.

    Dispatches based on the ``method`` field in the JSON-RPC 2.0 request body.
    Capabilities are read directly from the manifest YAML ``capabilities.tools``,
    ``capabilities.resources``, and ``capabilities.prompts`` sections.

    Path Parameters:
        - filename: Manifest YAML filename (e.g., mcp_weather.yaml)
    """
    try:
        manifest = await load_manifest_from_file(filename)
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(
            status_code=404,
            detail={
                "error": f"Manifest file not found: {filename}",
                "available_manifests": list_available_manifests(),
            },
        ) from e

    # Parse JSON-RPC request body
    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        body = {}

    method = body.get("method", "tools/list")
    request_id = body.get("id", 1)
    identity = manifest.get("identity", {})

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"tools": _get_mcp_tools(manifest)},
        }

    if method == "resources/list":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"resources": _get_mcp_resources(manifest)},
        }

    if method == "prompts/list":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"prompts": _get_mcp_prompts(manifest)},
        }

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": manifest.get("protocol", {}).get(
                    "version", "2025-11-25"
                ),
                "capabilities": {},
                "serverInfo": {
                    "name": identity.get("name", "TestAgent"),
                    "version": identity.get("version", "1.0.0"),
                },
            },
        }

    # Generic fallback for any other method
    return {"jsonrpc": "2.0", "id": request_id, "result": {}}


@app.get("/agents/{filename}/.well-known/agent.json")
async def a2a_agent_card(filename: str):
    """
    A2A Agent Card endpoint for testing.

    Returns an A2A agent card built from the manifest's ``capabilities.skills``
    section and top-level identity/security fields.

    Path Parameters:
        - filename: Manifest YAML filename (e.g., a2a_logistics.yaml)
    """
    try:
        manifest = await load_manifest_from_file(filename)
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(
            status_code=404,
            detail={
                "error": f"Manifest file not found: {filename}",
                "available_manifests": list_available_manifests(),
            },
        ) from e

    return _build_agent_card(filename, manifest)


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "healthy", "available_manifests": list_available_manifests()}


@app.post("/upload")
async def upload_manifest(
    file: Annotated[UploadFile, File(description="Manifest YAML file")],
    filename: str = Form(..., description="Manifest filename"),
):
    """
    Upload a new manifest.

    This endpoint accepts a YAML file and stores it in the fixtures directory,
    making it immediately available via the /manifest endpoint.

    Form Parameters:
        - file: YAML manifest file
        - filename: Manifest filename (e.g., 'a2a_logistics.yaml')

    Returns:
        Details of the uploaded manifest

    Example:
        curl -X POST http://localhost:9000/upload \\
          -F "file=@my_manifest.yaml" \\
          -F "filename=my_custom_agent.yaml"
    """
    if not filename.endswith(".yaml"):
        raise HTTPException(
            status_code=400, detail={"error": "Filename must end with .yaml extension"}
        )
    try:
        fixture_file = _fixture_path(filename)
    except ValueError as e:
        raise HTTPException(status_code=400, detail={"error": str(e)}) from e

    if filename in list_available_manifests():
        raise HTTPException(
            status_code=409,
            detail={
                "error": f"Manifest file '{filename}' already exists",
                "available_manifests": list_available_manifests(),
            },
        )

    try:
        # Ensure fixtures directory exists
        FIXTURES_DIR.mkdir(parents=True, exist_ok=True)

        # Read file content
        content = await file.read()
        yaml_content = content.decode("utf-8")

        # Validate YAML
        try:
            manifest = yaml.safe_load(yaml_content)
            if not isinstance(manifest, dict):
                raise ValueError("Manifest must be a YAML object")
        except yaml.YAMLError as e:
            raise HTTPException(
                status_code=400, detail={"error": f"Invalid YAML: {str(e)}"}
            ) from e

        # Save to fixtures directory
        with fixture_file.open("w") as f:
            f.write(yaml_content)

        return {
            "status": "success",
            "filename": filename,
            "manifest_name": manifest.get("identity", {}).get("name", "Unknown"),
            "file_path": str(fixture_file),
            "endpoint": f"/manifest?filename={filename}",
            "message": f"Manifest '{filename}' uploaded and ready to serve",
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail={"error": f"Failed to upload manifest: {str(e)}"}
        ) from e


@app.delete("/manifests/{filename}")
async def delete_manifest(filename: str):
    """
    Delete a manifest file.

    This endpoint deletes a manifest file from the fixtures directory.

    Path Parameters:
        - filename: Manifest filename to delete

    Example:
        curl -X DELETE http://localhost:9000/manifests/a2a_logistics.yaml
    """
    if filename not in list_available_manifests():
        raise HTTPException(
            status_code=404,
            detail={
                "error": f"Manifest file '{filename}' not found",
                "available_manifests": list_available_manifests(),
            },
        )

    try:
        # Delete file
        fixture_file = _fixture_path(filename)
        if fixture_file.exists():
            fixture_file.unlink()

        return {
            "status": "success",
            "filename": filename,
            "message": f"Manifest '{filename}' deleted",
        }
    except Exception as e:
        raise HTTPException(
            status_code=500, detail={"error": f"Failed to delete manifest: {str(e)}"}
        ) from e


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "manifest_server:app", host="0.0.0.0", port=9000, reload=True, log_level="info"
    )
