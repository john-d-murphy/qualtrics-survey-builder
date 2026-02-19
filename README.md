# qualtrics-survey-builder

YAML-driven survey builder for the Qualtrics API.

Define your survey as a YAML file — questions, logic, flow, consent — and deploy it to Qualtrics with a single command.

## Features

- **Declarative YAML schema** — define questions, blocks, display logic, screen-outs, and flow in one file
- **Question types** — single select, multi-select, dropdown, matrix (Likert), bipolar scales, sliders, text entry, descriptive
- **Display logic** — conditionally show/hide questions and entire blocks
- **Screen-outs** — end the survey early based on responses
- **Flow control** — block ordering, balanced randomization for within-subjects designs
- **Consent blocks** — inline HTML or a built-in template
- **Dry-run mode** — generate the full survey JSON locally without touching the API
- **Word export** — render the YAML spec as a formatted .docx for review or IRB submission

## Installation

```bash
pip install pyyaml requests python-docx
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv add pyyaml requests python-docx
```

## Quick Start

### 1. Define your survey

```yaml
# survey.yaml
version: 2
meta:
  name: "My Research Survey"
  language: "EN"

consent:
  source: inline
  html: |
    <p>You are being invited to participate in a research study...</p>

blocks:
  - id: demographics
    title: "Demographics"
    questions:
      - id: age
        type: dropdown
        text: "What is your age range?"
        choices: ["18–24", "25–34", "35–44", "45–54", "55+"]
        force_response: true

      - id: role
        type: multi_select
        text: "Which best describes you?"
        choices: ["Student", "Researcher", "Professional", "Other"]
        allow_other: true
        max_choices: 2

flow:
  order:
    - block: consent
    - block: demographics
```

### 2. Deploy to Qualtrics

```bash
# Set your credentials
export QUALTRICS_BASE_URL="https://your-org.qualtrics.com"
export QUALTRICS_API_TOKEN="your-token-here"

# Build and deploy
python qualtrics_survey_generator.py \
    --yaml survey.yaml \
    --out survey_definition.json
```

### 3. Or dry-run locally

```bash
python qualtrics_survey_generator.py \
    --yaml survey.yaml \
    --out survey_definition.json \
    --dry-run
```

### 4. Export to Word

```bash
python convert_yaml_to_word.py \
    --yaml survey.yaml \
    --out survey_questions.docx
```

## YAML Schema

### Meta

```yaml
meta:
  name: "Survey Title"
  description: "Optional description"
  language: "EN"
  project_category: "CORE"

  slider_defaults:       # applied to all slider questions
    min: 1
    max: 7
    grid_lines: 7

  bipolar_defaults:      # applied to all bipolar scales
    points: 7
    middle_label: "Neither"
```

### Question Types

| Type | Description | Key Fields |
|------|-------------|------------|
| `single_select` | Radio buttons (choose one) | `choices`, `allow_other` |
| `multi_select` | Checkboxes (choose many) | `choices`, `allow_other`, `min_choices`, `max_choices` |
| `dropdown` | Dropdown menu | `choices` |
| `matrix` | Likert grid (rows × columns) | `rows`, `columns` |
| `bipolar_group` | Semantic differential scales | `pairs`, `points` |
| `bipolar_matrix` | Same as bipolar_group (alias) | `pairs`, `points` |
| `slider_group` | Multiple sliders | `items`, `labels` |
| `text_entry` | Open text (multi-line or single) | `subtype: single_line` |
| `descriptive` | HTML info block (no response) | `html` |

### Display Logic

Show a question only when another question has a specific answer:

```yaml
- id: followup
  type: text_entry
  text: "Please elaborate:"
  display_logic:
    condition: { question: interest, selected: "Yes" }
```

Or show when a choice is *not* selected:

```yaml
  display_logic:
    condition: { question: tools_used, is_not: "None" }
```

Display logic also works at the block level:

```yaml
- id: advanced_block
  title: "Advanced Questions"
  display_logic:
    condition: { question: experience, is_not: "Beginner" }
  questions: [...]
```

### Screen-Outs

End the survey if a condition is met:

```yaml
- id: eligible
  type: single_select
  text: "Are you 18 or older?"
  choices: ["Yes", "No"]
  screen_out:
    condition: { selected: "No" }
    message: "You must be 18+ to participate."
```

### Flow

Control block ordering and randomization:

```yaml
flow:
  order:
    - block: consent
    - block: demographics
    - block: main_task
    - randomizer:
        source: algorithm_blocks
        present_count: 3
        evenly_present: true
    - block: debrief
```

### Algorithm Rating Blocks

For within-subjects designs with counterbalanced conditions:

```yaml
algorithms:
  names: ["ConditionA", "ConditionB", "ConditionC"]
  rating_style: bipolar
  ratings:
    usability:
      title: "Usability"
      items: ["Easy to use", "Intuitive", "Efficient"]
    quality:
      title: "Quality"
      items: ["Accurate", "Reliable", "Useful"]
```

## CLI Options

```
qualtrics_survey_generator.py:
  --base-url URL     Qualtrics base URL (or set QUALTRICS_BASE_URL)
  --api-token TOKEN  API token (or set QUALTRICS_API_TOKEN)
  --yaml PATH        YAML survey spec
  --out PATH         Output JSON path
  --dry-run          Generate JSON locally, no API calls
  --dump-flow PATH   Dump flow payload for debugging
  --simple-flow      Skip branch logic (linear blocks only)

convert_yaml_to_word.py:
  --yaml PATH        YAML survey spec
  --out PATH         Output .docx path
```

## Troubleshooting

**Flow PUT returns 500**: Use `--dump-flow flow.json` to inspect the payload. If it persists, the builder will automatically fall back to a simple linear flow and log the desired block order for manual arrangement in the Qualtrics UI.

**Question creation returns 400**: The error response includes `validationErrors` with specific field-level messages. Check that choice counts, validation types, and question structures match the [Qualtrics API v3 schema](https://api.qualtrics.com/).

## License

MIT
