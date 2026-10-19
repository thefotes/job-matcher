# Candidate onboarding interview

After installing and connecting the job-matcher MCP, start a conversation with your agent and paste the prompt below. Supply your resume in that conversation or give the agent access to your local resume file. The evaluator accepts extracted text. Your profile stays in the local SQLite database. Evaluating jobs sends the resume, confirmed interview answers and job description to TypeSafe using your key; onboarding itself makes no TypeSafe calls.

## Reusable prompt

Help me build my private candidate profile using the job-matcher MCP. Read get_onboarding and its schema first (call it with include_resume=true and include_schema=true; it is compact by default and only returns the resume text and schema with those flags). If a profile exists, preserve it and resume the interview; do not reset it. If there is no resume, ask me for it, extract its text accurately and review any extraction problems. Explain what is stored locally and what is sent to TypeSafe during evaluation.

Review the target roles, location, remote requirements, base salary floor/target, currency, work authorization, preferences and dealbreakers with me. Do not assume any defaults apply to a new user: the profile ships empty, and evaluation refuses until the required fields are set. Clarify regular office attendance versus occasional travel, and how much hands-on work I want. Record those constraints in the existing profile fields using save_profile only after reviewing the complete profile with me. Read the saved profile again (with include_resume=true) before a replacement save so you preserve experience_answers, the resume and existing preferences. Resolve contradictions through a question rather than guessing.

Use the interview.pending_questions returned by get_onboarding. Read my resume before asking questions; summarize relevant evidence, then ask for corrections and additions, especially information my resume omits. Ask one or two focused questions per turn. Prioritize topics relevant to my target roles and any specific uncertain job requirements. Distinguish personal hands-on experience from leading a team with that experience; distinguish AI-assisted development from shipping AI/ML products. Ask for concrete examples, tools, scope, recency and outcomes when useful, without requiring metrics I cannot substantiate.

Save only my explicit statements or summaries I have confirmed. Do not derive years, technologies, duties, outcomes or proficiency levels and present them as confirmed interview facts. Save a concise factual summary of what I did (scope, team sizes, responsibilities, recency, outcomes), show it to me and ask whether it is accurate. Do not save my conversational hedging or self-deprecation verbatim; the evaluator reads hedges as weaker evidence. Record genuine limits plainly as facts (for example, "has not hired managers"). Use save_experience_answers with topic, status, statement and user_confirmed: true. Use reported for stated experience, no_experience only when I explicitly say I have none, and skipped only when I explicitly choose to skip. A skipped topic has an empty statement. These are user-reported assertions, not independently verified credentials.

The save tool merges by topic. Include existing confirmed detail when refining that topic; it replaces that topic's prior answer. Confirm any correction or removal with me. Get onboarding again after each save, verify the result and continue pending questions. If I stop, leave the interview resumable; do not mark remaining topics skipped on my behalf. Previously skipped topics can be revisited when I choose or when a job makes them relevant.

Before any evaluation, show me get_onboarding's consent.disclosure verbatim and ask whether I agree to send my profile to TypeSafe. Call record_consent with user_confirmed: true only if I explicitly agree; if I decline, say that evaluation stays unavailable and continue the rest of onboarding.

Ask how selective recommendations should be, using the strictness options from get_onboarding: wide_net, generous, balanced (default), selective or strict. Explain that it controls how much benefit of the doubt I get when a posting is ambiguous or my fit is uncertain, that confirmed dealbreakers are skipped at every setting, and that I can change it any time without new evaluations. Save it with set_strictness.

At the end, show a concise profile summary, missing/skipped topics, and any unresolved contradictions. Explain that interview completion means each topic was addressed or skipped, not that I meet every job requirement. Resume readiness and interview completion are separate. Do not promise that more profile detail eliminates all investigation flags. Do not evaluate jobs or send my profile externally during this interview unless I ask.

## Tool contract

`get_onboarding` returns the saved profile, resume readiness, schema and interview plan. `save_experience_answers` accepts 1–9 answers and merges by topic, preserving unrelated profile fields. An example:

```json
{
  "answers": [
    {
      "topic": "ai_development",
      "status": "reported",
      "statement": "I use an AI coding assistant for implementation and test drafts, then review and run the generated code myself.",
      "user_confirmed": true
    }
  ]
}
```

This is an example payload, not a claim about a particular user. `user_confirmed` is the calling agent's attestation that the user supplied or confirmed the statement; the server cannot prove that a conversation occurred. Keep a trusted agent in control of profile writes.

Topics are engineering_tenure, management_tenure, leadership_scope, hands_on, technical_scope, developer_tooling, ai_development, ai_products and domains. Target preferences and work constraints remain separate profile fields.

CLI users can retrieve the same interview plan with `job-matcher onboarding` and merge an array of confirmed answers with `job-matcher experience-save --input answers.json`. The existing `init` wizard performs basic resume/preferences setup; it does not conduct this conversational interview.
