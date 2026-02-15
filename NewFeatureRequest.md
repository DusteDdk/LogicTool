# Feature request: Better Dashboard Log
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


# Examples

- logic_set_rule:
  - Call: 💾 🛡️ 🐍 _no-air-control_ <COMPACT_JSON_LOG_ENTRY>
  - Reply: ✅ 🔁 ⏺️ _no-air-control_ 214ms <COMPACT_JSON_LOG_ENTRY>
- logic_remove_rule:
  - Call: 🗑️ 🛡️ 🆔 _no-air-control_ <COMPACT_JSON_LOG_ENTRY>
  - Reply: ✅ 🔁 ⏺️ _no-air-control_ 10ms <COMPACT_JSON_LOG_ENTRY>
- logic_set_bundle:
  - Call: 💾 📦 📜 _decl-collision_ <COMPACT_JSON_LOG_ENTRY>
  - Reply: ✅ 🔁 🎉 _decl-collision_ 12ms <COMPACT_JSON_LOG_ENTRY>
- logic_remove_bundle:
  - Call: 🗑️ 📦 🆔 _decl-collision_ <COMPACT_JSON_LOG_ENTRY>
  - Reply: ✅ 🔁 ⏺️ _decl-collision_ 9ms <COMPACT_JSON_LOG_ENTRY>
- logic_set_expectation:
  - Call: 💾 🎯 🧾 _exp-grounded-uses-normals_ <COMPACT_JSON_LOG_ENTRY>
  - Reply: ✅ 🔁 🎉 _exp-grounded-uses-normals_ 18ms <COMPACT_JSON_LOG_ENTRY>
- logic_remove_expectation:
  - Call: 🗑️ 🎯 🆔 _exp-grounded-uses-normals_ <COMPACT_JSON_LOG_ENTRY>
  - Reply: ✅ 🔁 ⏺️ _exp-grounded-uses-normals_ 7ms <COMPACT_JSON_LOG_ENTRY>
- logic_check:
  - Call: 🧪 🔧 🤔 _hypothesis_ <COMPACT_JSON_LOG_ENTRY>
  - Reply: ✅ 🔁 💥 _hypothesis_ 26ms <COMPACT_JSON_LOG_ENTRY>
- logic_context_patch:
  - Call: 💾 🧬 🆔 _c_slope_grounding_ <COMPACT_JSON_LOG_ENTRY>
  - Reply: ✅ 🔁 ⏺️ _c_slope_grounding_ 15ms <COMPACT_JSON_LOG_ENTRY>
- logic_list:
  - Call: 👀 🔧 🔧 _none_ <COMPACT_JSON_LOG_ENTRY>
  - Reply: ✅ 🔁 ⏺️ _none_ 6ms <COMPACT_JSON_LOG_ENTRY>
