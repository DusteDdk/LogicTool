# IMPORTANT
- DO NOT EDIT ANY OTHER FILES YET!

# Prompt
- ONLY UPDATE THE ICONS in the Examples section!

# Feature request: Better Log
Update the supervisor dashboard per-session log:
- Remove the expandable card.

The new log should not have any headline or text outside the content.
The content should be a sliding window of the 10 newest entries in the log for that session.
Each line should be the compact json lines as it is today, line-wrap should be disabled for this (full line should be available in the box, but just clipped, so user could still copy it out by selecting the text and copying it).
The log should be presented in an element that fills the card, and be surrounded by thin discretely colored borders on top and bottom but no borders on the sides.
Log JSON format should be updated to include the duration of the request (from call received to reply sent) (in whole milliseconds, not more exact)

See ./SupervisorIcons.md for the particular icons to use for each:

Lines for a tool call should be prefixed with the symbols (UTF-8 emoji) visually indicating the action along with the identifier of the item they work on (if provided) so the line follows the template:
"<Operation> <Tool> <Language> <itemId> <JSON_log_line>"

Lines for a tool-response should follow this template:
"<ToolCallResult> <Reply_symbol> <LogicResult> <itemId> <requestDuration> <JSON_log_line>


Examples:
- logic_set_rule:
  - Call: 💾 🛡️ 📜 _no-air-control_ <COMPACT_JSON_LOG_ENTRY>
  - Reply: 🟩 🔁 🛡️  🎉 214ms <COMPACT_JSON_LOG_ENTRY>
logic_remove_rule:
  - Call: 🗑️ 🛡️ 🆔 _no-air-control_ <COMPACT_JSON_LOG_ENTRY>
  - Reply:🟩 🔁 ⏺️ 10ms <COMPACT_JSON_LOG_ENTRY>
logic_set_bundle
logic_remove_bundle
logic_set_expectation
logic_remove_expectation
logic_check
logic_context_patch
logic_list
