# Terms Guide (Plain Language)

## What this tool is for
This Logic MCP tool is a persistent "reasoning notebook" for technical constraints.
You store declarations and rules once, then run checks to test whether a proposed change causes contradictions or breaks expected relationships.

## Tool names
This server exposes these tools:
- `logic_set_rule`
- `logic_remove_rule`
- `logic_set_bundle`
- `logic_remove_bundle`
- `logic_set_expectation`
- `logic_remove_expectation`
- `logic_check`
- `logic_context_patch`
- `logic_list`
- `logic_read`

---

## Symbol
A **Symbol** is a named variable used in logic statements, like `price`, `is_enabled`, or `retry_count`.

In this tool:
- Symbols are stored globally in the session under a symbol table.
- Each symbol has a type (`Int`, `Real`, or `Bool`).
- Types can come from:
  - SMT2 declarations in bundles/rules
  - Type inference from `pyexpr` rules
  - Typed placeholders or concrete values in check facts

Why it matters:
- Symbols are the shared vocabulary between bundles, rules, and checks.
- If symbol types conflict, checks fail early with type errors.

Story step:
You begin modeling a payment flow. You decide your key words are `gross_cents`, `fee_cents`, and `net_cents`. Those names become symbols, and their numeric types let later rules connect correctly.

---

## Bundle
A **Bundle** is a reusable block of SMT2 content, usually declarations and base constraints shared by many rules.

In this tool:
- Bundles are persistent items with IDs.
- Bundles are always stored with `lang = smt2`.
- Typical use: declare common symbols once and set fixed constants.

Why it matters:
- Bundles prevent repeating setup in every rule.
- They create a consistent foundation for all later checks.

Story step:
You add a "payment declarations" bundle that introduces the core symbols and a couple of fixed assumptions. Now every future rule can rely on that same base setup.

---

## Expectation
An **Expectation** is a relationship between two rules that should stay true as the system evolves.

In this tool:
- An expectation references two existing rule IDs: `a_ref` and `b_ref`.
- It has a kind:
  - `entails` meaning "if A is true, B must also be true"
  - `equivalent` meaning "A and B mean the same thing"
- Expectations are stored with internal `lang = expect`.

Why it matters:
- Expectations catch omission bugs and refactor drift.
- They verify that important implications still hold after edits.

Story step:
You mark that your "net formula rule" should imply your "payout safety rule." If someone edits one rule later and breaks that implication, expectation checks will flag it.

---

## Concepts
A **Concept** is a human-friendly explanation node that groups meaning around rules/expectations/code references.

In this tool:
- Concepts live in context (not as executable constraints).
- A concept includes:
  - `concept` (name)
  - `meaning` (plain explanation)
  - `primary_symbols`
  - links to related rule IDs, expectation IDs, and/or code binding IDs
- Concepts must not be "floating": they must connect to at least one real rule/expectation/binding, and those references must exist.

Why it matters:
- Concepts help humans understand why constraints exist.
- They turn raw logic into maintainable team knowledge.

Story step:
You create a concept called "Net settlement" and describe it in plain English. You link it to the symbols, rules, and expectation you already created, so another engineer can understand intent quickly.

---

## `lang` types and how they relate
This tool uses three language labels in practice:

1. `pyexpr`
- Python-like boolean/math expression format for rules.
- Easier to read/write for many engineers.
- Parsed and translated into solver constraints.

2. `smt2`
- SMT-LIB syntax used directly by the solver.
- Supports explicit declarations and solver-native assertions.
- Used by bundles and also by rules that need solver-level precision.

3. `expect`
- Internal storage lang for expectation objects.
- Not a free-form expression language.
- Represents structured links (`kind`, `a_ref`, `b_ref`) between rules.

How they relate:
- `smt2` and `pyexpr` both create executable rule constraints.
- `expect` does not create raw math constraints by itself; it checks relationships between rule constraints.
- Concepts have no `lang`; they are context/meaning metadata that point to rules and expectations.
- Symbols are shared across `pyexpr` and `smt2`, so both languages can reason over the same variables.

---

## One simple multi-step story (all terms together)
Step 1:
A team wants safer invoice logic. They define symbols for gross amount, fees, and net amount.

Step 2:
They add one bundle in `smt2` that declares those symbols and baseline assumptions.

Step 3:
They add a few rules, some in `pyexpr` (easy readability) and some in `smt2` (exact solver form).

Step 4:
They add an expectation: "rule A entails rule B," so regressions are caught automatically.

Step 5:
They add a concept named "Invoice settlement invariants" that explains the business meaning and links to the related rules and expectation.

Step 6:
During a refactor, a developer changes one rule. A check runs, the expectation fails, and the team immediately sees the semantic drift before shipping.
