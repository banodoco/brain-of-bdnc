"""LLM-based grant application assessment."""

import json
import logging

from src.features.grants.pricing import GPU_RATES, MAX_GRANT_USD, calculate_grant_cost

logger = logging.getLogger('DiscordBot')

SYSTEM_PROMPT = """You are a grant reviewer for compute micro-grants (10-50 GPU hours) for open-source AI projects.

You review applications and decide whether to approve, reject, or request more information.

## Required Application Info
- Project description: what the project does
- Compute purpose: what the GPU hours will be used for (training, fine-tuning, inference, etc.)
- Links to prior work: GitHub repos, papers, demos, or other evidence of capability

## Approval Criteria
- Project must be open-source (or commit to open-sourcing results)
- Reasonable scope: 10-50 GPU hours should meaningfully advance the project
- Demonstrated capability or worth a YOLO: prior experience is nice but not required
- Community benefit: project serves the broader AI/ML community

## Available GPU Types and Rates
{gpu_info}

## Budget Cap
The maximum grant is capped at ${max_grant_usd:.0f} USD (equivalent to 50 hours of H100).
The applicant may request a specific GPU type or hours — honour their preference if reasonable, but the total cost must not exceed the cap.
If they don't specify, choose based on project needs.

## Prior Grant History
If the applicant has received grants before, this will be noted below the application.
Be VERY hesitant to approve someone who already has an open/active grant (status: reviewing, awaiting_wallet).
For applicants with past paid grants, apply higher scrutiny — they should demonstrate clear results from previous grants before receiving more.
First-time applicants with no history should be evaluated normally.

## Response Format
Return ONLY valid JSON (no markdown, no code fences) with these exact fields:

{{"reasoning": "your internal analysis of the application (2-4 sentences — project viability, applicant capability, scope assessment)", "decision": "approved" | "rejected" | "needs_info" | "spam", "response": "message to show the applicant (2-4 sentences — friendly, constructive)", "gpu_type": "H100_80GB" | "H200" | "B200" | null, "recommended_hours": <number 10-50 or null>}}

- "reasoning": your private assessment rationale (stored in DB, not shown to applicant)
- "decision": one of "approved", "rejected", "needs_info", "spam"
- "response": the public-facing message shown to the applicant (not used for spam — thread is deleted)
- "gpu_type": required for "approved", null otherwise
- "recommended_hours": required for "approved" (10-50), null otherwise

Use "spam" for posts that are clearly not real applications — e.g. test posts, gibberish, jokes, off-topic messages, or obvious low-effort spam. These threads will be silently deleted."""


def _build_system_prompt() -> str:
    gpu_info = '\n'.join(f'- {name}: ${rate:.2f}/hr' for name, rate in GPU_RATES.items())
    return SYSTEM_PROMPT.format(gpu_info=gpu_info, max_grant_usd=MAX_GRANT_USD)


def _parse_json(text: str) -> dict:
    """Extract JSON from LLM response, stripping markdown fences if present."""
    cleaned = text.strip()
    if cleaned.startswith('```'):
        cleaned = cleaned.split('\n', 1)[1] if '\n' in cleaned else cleaned[3:]
        if cleaned.endswith('```'):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
    return json.loads(cleaned)


def _validate(result: dict) -> str | None:
    """Validate assessment structure. Returns error string or None if valid."""
    required = ['reasoning', 'decision', 'response']
    for field in required:
        if field not in result or not isinstance(result[field], str) or not result[field].strip():
            return f"Missing or empty required field: '{field}'"

    if result['decision'] not in ('approved', 'rejected', 'needs_info', 'spam'):
        return f"Invalid decision: '{result['decision']}'. Must be 'approved', 'rejected', or 'needs_info'"

    if result['decision'] == 'approved':
        if not result.get('gpu_type') or result['gpu_type'] not in GPU_RATES:
            return f"Invalid gpu_type: '{result.get('gpu_type')}'. Must be one of {list(GPU_RATES.keys())}"
        hours = result.get('recommended_hours')
        if not hours or not isinstance(hours, (int, float)) or not (10 <= hours <= 50):
            return f"Invalid recommended_hours: {hours}. Must be a number between 10 and 50"
        cost = calculate_grant_cost(result['gpu_type'], hours)
        if cost > MAX_GRANT_USD:
            return f"Grant cost ${cost:.2f} exceeds max ${MAX_GRANT_USD:.2f}. Reduce hours or pick a cheaper GPU"

    return None


async def assess_application(claude_client, thread_content: str, grant_history: list | None = None) -> dict:
    """Assess a grant application using Claude with structured output and retry.

    Returns:
        dict with keys: reasoning, decision, response, gpu_type, recommended_hours

    Raises:
        RuntimeError if all attempts fail
    """
    system_prompt = _build_system_prompt()

    user_content = f'Please review this grant application:\n\n{thread_content}'

    if grant_history:
        history_lines = []
        for g in grant_history:
            line = f"- {g['created_at'][:10]}: {g['status']}"
            if g.get('gpu_type'):
                line += f" | {g['gpu_type']} {g.get('recommended_hours', '?')}hrs"
            if g.get('total_cost_usd'):
                line += f" | ${g['total_cost_usd']}"
            history_lines.append(line)
        user_content += (
            f"\n\n---\n**PRIOR GRANT HISTORY FOR THIS APPLICANT:**\n"
            + '\n'.join(history_lines)
        )

    messages = [
        {'role': 'user', 'content': user_content}
    ]

    max_attempts = 3
    last_error = None

    for attempt in range(max_attempts):
        response_text = await claude_client.generate_chat_completion(
            model='claude-sonnet-4-20250514',
            system_prompt=system_prompt,
            messages=messages,
            max_tokens=1024,
            temperature=0.3,
        )

        # Try to parse
        try:
            result = _parse_json(response_text)
        except json.JSONDecodeError as e:
            last_error = f"Invalid JSON: {e}"
            logger.warning(f"Grant assessor attempt {attempt + 1}: {last_error}")
            # Feed error back for retry
            messages.append({'role': 'assistant', 'content': response_text})
            messages.append({'role': 'user', 'content': f"Your response was not valid JSON. Error: {e}\n\nPlease return ONLY valid JSON with the exact fields specified."})
            continue

        # Validate structure
        validation_error = _validate(result)
        if validation_error:
            last_error = validation_error
            logger.warning(f"Grant assessor attempt {attempt + 1}: {last_error}")
            # Feed error back for retry
            messages.append({'role': 'assistant', 'content': response_text})
            messages.append({'role': 'user', 'content': f"Your response had a validation error: {validation_error}\n\nPlease fix and return valid JSON."})
            continue

        # Success
        logger.info(f"Grant assessment succeeded on attempt {attempt + 1}: decision={result['decision']}")
        return result

    raise RuntimeError(f"Grant assessment failed after {max_attempts} attempts. Last error: {last_error}")
