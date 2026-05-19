# AgenticNetwork

This repository contains the current AgenticNetwork components. Each component
owns its own install and run instructions in its local README.

- `agent_directory_service/`: AGNTCY ADS operator tooling for starting the
  shared directory service, publishing Agent Cards, inspecting routes, and
  repairing stale routing state. See
  [`agent_directory_service/README.md`](agent_directory_service/README.md).
- `planner_agent/`: product planner/main-agent runtime. See
  [`planner_agent/README.md`](planner_agent/README.md).
- `email_agent/`, `coding_agent/`, `disaster_response_agent/`,
  `geoSpatial_agent/`, `intelligence_agent/`, and `scout_agent/`:
  specialized A2A agents with their own setup and run instructions in each
  folder.

Do not install one component's requirements file into another component's virtual environment.
