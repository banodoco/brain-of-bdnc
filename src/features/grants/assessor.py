"""LLM-based grant application assessment."""

import json
import logging

from src.features.grants.pricing import GPU_RATES

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
- Demonstrated capability: applicant has relevant experience or prior work
- Community benefit: project serves the broader AI/ML community

## Available GPU Types and Rates
{gpu_info}

## Prior Grant History
If the applicant has received grants before, this will be noted below the application.
Be VERY hesitant to approve someone who already has an open/active grant (status: reviewing, awaiting_wallet).
For applicants with past paid grants, apply higher scrutiny — they should demonstrate clear results from previous grants before receiving more.
First-time applicants with no history should be evaluated normally.

## Response Format
Return ONLY valid JSON (no markdown, no code fences):
{{"status": "approved" | "rejected" | "needs_info", "explanation": "brief explanation for the applicant", "gpu_type": "H100_80GB" | "H200" | "B200" | null, "recommended_hours": <number 10-50 or null>}}

- For "approved": include gpu_type and recommended_hours based on project needs
- For "needs_info": explain what specific information is missing
- For "rejected": explain why clearly and constructively
- Keep explanations concise (2-4 sentences)"""


def _build_system_prompt() -> str:
    gpu_info = '\n'.join(f'- {name}: ${rate:.2f}/hr' for name, rate in GPU_RATES.items())
    return SYSTEM_PROMPT.format(gpu_info=gpu_info)


async def assess_application(claude_client, thread_content: str, grant_history: list | None = None) -> dict:
    """Assess a grant application using Claude.

    Args:
        claude_client: ClaudeClient instance (from bot.claude_client)
        thread_content: The full text of the forum post
        grant_history: List of past grant dicts for this applicant (optional)

    Returns:
        dict with keys: status, explanation, gpu_type, recommended_hours

    Raises:
        RuntimeError if LLM call fails or returns invalid JSON
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

    response_text = await claude_client.generate_chat_completion(
        model='claude-sonnet-4-20250514',
        system_prompt=system_prompt,
        messages=messages,
        max_tokens=1024,
        temperature=0.3,
    )

    # Strip any markdown fences the model might add
    cleaned = response_text.strip()
    if cleaned.startswith('```'):
        cleaned = cleaned.split('\n', 1)[1] if '\n' in cleaned else cleaned[3:]
        if cleaned.endswith('```'):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError as e:
        logger.error(f"Grant assessor returned invalid JSON: {response_text}")
        raise RuntimeError(f"LLM returned invalid JSON: {e}")

    # Validate required fields
    if 'status' not in result or result['status'] not in ('approved', 'rejected', 'needs_info'):
        raise RuntimeError(f"Invalid assessment status: {result.get('status')}")

    if result['status'] == 'approved':
        if not result.get('gpu_type') or result['gpu_type'] not in GPU_RATES:
            raise RuntimeError(f"Invalid gpu_type for approved grant: {result.get('gpu_type')}")
        hours = result.get('recommended_hours')
        if not hours or not (10 <= hours <= 50):
            raise RuntimeError(f"Invalid recommended_hours: {hours} (must be 10-50)")

    return result
