"""Digital employee resource links, independent of the OpenAPI login service."""
def employee_access_url(employee, public_base_url="") -> str:
    return f"{public_base_url.rstrip('/')}/h5/agents/{employee.id}/chat"
