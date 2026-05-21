# Follow-Ups

## Add code execution

- See if we can give the model access to APIs to be executed by the agent, so only local commands would need to round-trip through the local app.
  - E.g. it would be cool to say "play new Lady Gaga" album, and then it could figure that out via the Qobuz API and just send the final ID back to the client.
- Also for tasks which LLMs can not handle, e.g. find the most listened-to song from last month. Even with a tool for the play history available, it'd need a bit of code to reliably count the top playing one.

## Eval case refinements

- More detailed tool use assertions (e.g. parameters)
- More specific final checks; unique/inline per test case, for improved readability?
