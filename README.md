# SIGAIDA Agentic Travel Planner Project

## Project Details

TravelAgentic is an AI-powered travel planner that helps budget-conscious college students find the cheapest way to get where they're going. A user enters a trip, such as Chicago to New York, and the agent asks follow-up questions about budget, travel mode, rental cars, and activities. It then returns real, bookable flights, hotels, and rental cars from the Duffel API, along with charts showing which dates and months tend to be cheaper based on historical data. Behind the scenes, a LangGraph system coordinates specialized agents for historical price trends, live pricing, hotel preference matching, and a final evaluator that combines their results. Every price shown comes from live API data or the team's own historical database, never from the language model's guesswork.

### Members

| Name |
|------|
| Arjun Agarwal|
| Saatvik Palli |
| Inigo Serrano     |
| Wilson Zhu      |
| Gia Bao Ta     |
| Ethan Guo     |
| Duc Vo     |
| Ethan Lin     |
| Alex Lien     |

### Datasets

- DOT Consumer Airfare Report Table 6 (Departure, Arrival, Dates, Quarter (Q1, Q2, Q3, Q4), avg_price, lowest_price)
- Kaggle Flight Prices (supplemental for V2)
- Dotlas for Hotels

### ML Model
- This model will classify user queries on whether they are related to travel or not to avoid unnecessary LLM API costs.
- Inputs: text
- Outputs: True/False

## Getting Started

This guide walks you from a fresh machine to a running project. Commands are for macOS/Linux; Windows equivalents are noted where they differ.

### 1. Install Git

macOS: run `git --version` (it offers to install if missing). Windows: install from [git-scm.com](https://git-scm.com/downloads).

### 2. Set up Git and GitHub access

Tell Git who you are (use the email on your GitHub account):

```bash
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
```

Create an SSH key and add it to GitHub (skip if you already have one in `~/.ssh/`):

```bash
ssh-keygen -t ed25519 -C "you@example.com"   # press Enter to accept the defaults
cat ~/.ssh/id_ed25519.pub                     # copy the output
```

Paste the output at **GitHub → Settings → SSH and GPG keys → New SSH key**, then confirm it works:

```bash
ssh -T git@github.com   # should print "Hi <username>! You've successfully authenticated..."
```

> You also need to accept the collaborator invite for this repo (check your email or GitHub notifications).

### 3. Clone the repo

```bash
git clone git@github.com:aagarwal32/TravelAgentic.git
cd TravelAgentic
```

Then follow the [Backend](#backend) and/or [Frontend](#frontend) setup below.

## Backend

All commands in this section run from `backend/`:

```bash
cd backend
```

### 1. Install Python

Python 3.13+ — download from [python.org](https://www.python.org/downloads/) or, on macOS, `brew install python@3.13`. Check with `python3 --version`.

### 2. Create a virtual environment and install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Run `source .venv/bin/activate` again in every new terminal. In VS Code, select the interpreter via **Cmd/Ctrl+Shift+P → Python: Select Interpreter → Enter interpreter path** → `backend/.venv/bin/python`.

### 3. Create your `.env` file

`.env` holds secrets and is not committed, so each person makes their own in `backend/`:

```env
SECRET_KEY=<generate one, see below>
ACCESS_TOKEN_EXPIRE_MINUTES=30
DEBUG=True
API_PREFIX=/api
DATABASE_URL=sqlite:///./database.db
ALLOWED_ORIGINS=http://localhost:3000
```

Generate a `SECRET_KEY` with:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Ask a teammate for any shared values (e.g. API keys) — never commit them.

### 4. Download the datasets

Datasets are too large for Git, so they're fetched by a script into `backend/datasets/`:

```bash
python scripts/download_datasets.py          # all datasets
python scripts/download_datasets.py --list   # see what's available
```

The local database is also not committed; everyone builds their own from the datasets.

### 5. Run the server

```bash
python main.py
```

Open http://localhost:8000/docs to see the API.

### After pulling changes

```bash
pip install -r requirements.txt       # if requirements.txt changed
python scripts/download_datasets.py   # if new datasets were added
```

## Frontend

_Coming soon._

## Day-to-Day Git Workflow

### 1. Pick an issue and create its branch on GitHub
- Go to the **Issues** tab and pick an issue assigned to you (filter with `assignee:@me`).
- In the issue's right sidebar, under **Development**, click **Create a branch**.
- Keep GitHub's suggested name. It starts with the issue number followed by the issue title
  (e.g. `<issue-number>-<short-description>`).

### 2. Switch to the branch, activate the environment, and update
```bash
git fetch origin                     # let your computer know the new branch exists
git checkout <branch-name>           # switch to the branch GitHub created

source .venv/bin/activate            # Mac/Linux
# .venv\Scripts\activate             # Windows

git pull                             # latest changes on your branch
git pull origin main                 # bring in work already merged into main
pip install -r requirements.txt      # install any packages teammates added
```

### 3. Work on the feature and commit as you go
```bash
git add <files>
git commit -m "<short description of the change>"
```
Commit after each working step, not only at the end.
Run `git pull origin main` every day or two to stay up to date and avoid merge conflicts.

### 4. Update requirements (only if you installed new packages), then push
```bash
pip freeze > requirements.txt
git diff requirements.txt            # check that only packages your feature needs were added
git add requirements.txt
git commit -m "Update requirements"
git push -u origin <branch-name>
```

### 5. Open a Pull Request on GitHub
- Write `Closes #<issue-number>` in the PR description so the issue closes automatically when merged.
- Request a review from a teammate. Never push directly to `main`.

### Command reference
| Command | What it does |
|---|---|
| `git fetch origin` | Downloads info about new branches and commits from GitHub; does **not** change your files |
| `git pull` | Downloads and applies new commits on **your current branch** |
| `git pull origin main` | Downloads the latest `main` and merges it **into your current branch** (you stay on your branch) |

## Textbox trip-intake demo (issue #4)

From the repository root, activate your Python environment and run:

```bash
source .venv/bin/activate
cd backend
python -m services.agent.graph
```

This reuses the `flight-agent` ChatGPT OAuth sign-in and streaming model call.
The first run opens browser sign-in and lists available models; choose a model
from that list. You can also supply an available slug with `--model <slug>`.
No OpenAI API key is required. Account/model access must support the existing
OAuth flow. Credentials are stored under `services/agent/.chatgpt-auth/`, ignored
by Git, and never enter graph state.

Enter a trip request, then answer follow-up questions in the same terminal input.
The model proposes JSON field updates; Python validates and merges them before
checking required details. Invalid values produce follow-up questions. Enter
`/quit` at a follow-up to exit. Completion prints validated constraints and calls
a placeholder planning node; it does not search or book travel.

The graph uses in-memory checkpoints: conversation state is retained during the
process but lost on restart. A future chat route can start a conversation with
`graph.invoke(initial_state, config)` and resume an interrupt with
`graph.invoke(Command(resume=text), config)`, using the same unique thread ID.
This demo uses one local OAuth account and is not a multi-user server endpoint.

Offline verification from the repository root:

```bash
PYTHONPATH=backend .venv/bin/python -m unittest discover -s backend/tests -v
```

The demo generates `backend/services/agent/trip_intake_graph.png` at startup.
To render it without OAuth or model calls, run from `backend/`:

```bash
python -m services.agent.graph --graph-only
```

PNG rendering uses the online Mermaid service and requires network access.
Only the graph topology is sent; the generated image is ignored by Git.

## Load DOT Table 6

Historical airfares (DOT Consumer Airfare Report, Table 6, 2022 and newer) used by
the trends agent. From `backend/`:

```bash
python scripts/download_datasets.py dot_table6   # downloads datasets/dot_table6.csv
python -m scripts.ingest_table6                  # loads it into dot_table6_fares
```

The second command replaces the table's contents, so running it twice never creates
duplicates. When DOT publishes a new quarter, run both commands again. It prints the
rows loaded, rows skipped and the newest year/quarter.

By default the script reads `datasets/dot_table6.csv`. To load a CSV from somewhere
else, pass its path with the optional `--csv` argument:

```bash
python -m scripts.ingest_table6 --csv path/to/dot_table6.csv
```
