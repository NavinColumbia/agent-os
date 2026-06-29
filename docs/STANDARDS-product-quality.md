# Standard: Product Quality — the CRAFT Checklist

Status: REQUIRED for every user-facing screen, form, and data view before it can be marked done or
pass review. Applies to the builders who produce UI (design-ux, brand-designer, frontend-engineer,
fullstack-engineer) and to the gates that sign it off (reviewer, qa-security, sdet). Companion to
docs/STANDARDS-qa.md (human-paced + idle-dwell verification) — that doc covers *timing* bugs; this doc
covers *completeness* bugs.

## Why this exists

"It renders and the happy path submits" keeps passing screens that a real person cannot actually use:
a sign-up form with no confirm-password field and no way to see what you typed, a password box that
silently blocks paste, a data table that shows a blank rectangle when the list is empty, an error that
reads `ERR_ENUM_4 / {"code":"VALIDATION_FAILED"}`. None of these throw a console error. All of them are
incomplete, and incomplete is a defect.

This checklist is the bar a senior product designer/engineer enforces. A screen is not done because it
exists; it is done when every item below is satisfied or an explicit, justified exception is recorded.
Reviewers: an unchecked item is a blocking comment, not a nitpick.

> A screen that "works when you do everything right" is half a screen. The other half is what happens
> when the user pauses, fat-fingers, pastes, tabs, hits Enter, arrives with no data, or loses the
> network. Ship both halves or it isn't shipped.

---

## 1. FORMS

Every form (sign-up, sign-in, reset, settings, checkout, any input collection) MUST satisfy:

- **Confirm-password on account creation.** Sign-up (and change-password) has a second "confirm
  password" field that is validated to match; mismatch shows a clear inline error and blocks submit.
- **Show/hide-password toggle.** Every password field has a reveal toggle (eye icon or "Show"
  text-button). The toggle is itself keyboard-operable and labelled (e.g. `aria-label="Show password"`
  / `aria-pressed`), and toggling does not lose the typed value or move the cursor.
- **Password-strength indicator.** New-password fields show live strength feedback (weak/fair/strong
  meter or rule checklist) and state the actual requirements in plain text ("at least 8 characters,
  one number") — never a bare "invalid" after the fact.
- **Inline + on-blur validation.** Validate a field when the user leaves it (on blur) and update inline
  as they correct it — do not wait until submit to reveal every problem at once, and do not validate
  so aggressively that an error flashes while the user is still typing the first character.
- **Clear, specific errors.** Each error says what is wrong and how to fix it, anchored to the field it
  refers to ("Email is missing an @", "Passwords don't match", "This email is already registered —
  sign in instead"). Never a generic "Invalid input", never a raw enum/JSON/stack/HTTP code.
- **Disabled submit while pending + spinner.** On submit, disable the submit button immediately
  (prevents double-submit) and show an in-button spinner or "Signing in…" state. Re-enable on
  success/failure. The button must never be clickable twice for one intent.
- **Autocomplete attributes.** Inputs carry correct `autocomplete` so password managers and browsers
  work: `email` → `autocomplete="email"`; sign-in password → `autocomplete="current-password"`;
  sign-up / reset new password → `autocomplete="new-password"`; name → `autocomplete="name"`; one-time
  codes → `autocomplete="one-time-code"`. Correct `type` too (`type="email"`, `type="password"`).
- **Autofocus the first field.** On load the first meaningful input is focused so the user can type
  immediately without reaching for the mouse (do not autofocus a field below the fold or on a screen
  where it would scroll-jump on mobile).
- **Enter-to-submit.** Pressing Enter from any field submits the form (real `<form>` with an
  `onSubmit` / submit-type button — not a click-only `<div>`). Enter must trigger the same validation
  and pending state as the button.
- **Paste allowed.** Never block paste, cut, copy, or autofill on any field — especially password,
  confirm-password, email, and OTP. `onPaste={e => e.preventDefault()}` and equivalents are forbidden;
  they break password managers and are a known anti-pattern that lowers security, not raises it.
- **No data loss on transient failure.** A failed submit (network/validation) keeps everything the
  user typed; it never clears the form. Re-render/refetch must not wipe in-progress input (see
  docs/STANDARDS-qa.md).

## 2. AUTH AFFORDANCES

- **Forgot/reset path — or an honest note.** A sign-in screen has a visible "Forgot password?" link
  that leads to a working reset flow. If reset is genuinely not built yet, say so honestly and give a
  real next step ("Password reset isn't available yet — email support@… to recover your account") —
  never a dead link, never a button that does nothing.
- **Remember-me / session honesty.** If sessions persist, offer a "Keep me signed in" control or state
  the session policy plainly. Do not silently log people out, and do not promise "remember me" the
  backend doesn't honor. Match the control to actual behavior.
- **Sign-in ⇄ sign-up switch clarity.** Each auth screen makes the alternative obvious and correctly
  labelled: sign-in shows "New here? Create an account"; sign-up shows "Already have an account? Sign
  in". The switch links to the right screen and the primary button text matches the screen's intent
  ("Create account" vs "Sign in" — never an ambiguous "Submit").

## 3. ACCESSIBILITY (WCAG 2.2 AA floor)

- **Every input has an associated `<label>.`** A real `<label for=…>` or wrapping label — placeholder
  text is NOT a label (it vanishes on focus and fails screen readers). Icon-only buttons get an
  `aria-label`.
- **Keyboard operable.** Every control (inputs, toggles, links, custom buttons, menus, modals) is
  reachable and activatable by keyboard alone, in a sensible tab order; no keyboard trap; Esc closes
  modals/menus.
- **Visible focus.** A clearly visible focus indicator on every focusable element — never
  `outline: none` without an equally visible replacement.
- **Sufficient contrast.** Text meets WCAG AA (4.5:1 body, 3:1 large/UI), measured with a checker, not
  eyeballed. Error text, placeholder, and disabled states are checked too.
- **ARIA where needed (and not where it isn't).** Use semantic HTML first; add ARIA only to fill gaps:
  `aria-invalid` on errored fields, `aria-busy`/`aria-live` for async/loading regions, `role`/`aria-
  expanded` on custom widgets. Don't ARIA-decorate native elements that already convey the role.
- **Error text linked to its field.** Each field's error is programmatically associated via
  `aria-describedby` (and the field marked `aria-invalid="true"`) so a screen reader announces the
  error when the user lands on the field — not just a visual red string floating nearby.

## 4. STATES — empty / loading / error for EVERY data view

Every screen or component that fetches or lists data MUST design and prove all of:

- **Empty / zero-data.** A purposeful empty state with a one-line explanation and, where relevant, a
  next action ("No projects yet — create your first one") — never a bare blank rectangle or a `0` with
  no context.
- **Loading.** A skeleton, spinner, or progress affordance while data is in flight — never a flash of
  empty-state, never a frozen screen with no signal. Slow networks must look intentional, not broken.
- **Error.** A recoverable error state with plain microcopy and a retry/back path ("Couldn't load your
  projects. Check your connection and try again." + a Retry button) — never a stuck spinner, a white
  screen, or a leaked raw error.
- **Partial / boundary.** Long strings, huge lists (pagination/virtualization), single-item, and
  oversized/under-sized inputs render without clipping or overflow.

A state with no design is a defect. "The happy path has data" does not exempt the other three.

## 5. MICROCOPY

- **Plain and specific.** Labels, buttons, empty states, and errors are written in human language that
  tells the user what happened and what to do next.
- **No raw machine output, ever.** No enum values, JSON blobs, stack traces, HTTP status numbers, or
  internal codes surfaced to the user. Map every backend error to a human sentence; log the raw detail
  for engineers, show the plain version to people.
- **No placeholder / lorem.** No "Lorem ipsum", "TODO", "Button", or "Title goes here" reaches a built
  screen.
- **Consistent voice.** Tone is consistent across the surface; capitalization and terminology for the
  same concept don't drift screen to screen ("Log in" vs "Sign in" — pick one).

## 6. CONSISTENCY

- **Buttons.** One visual language for primary/secondary/destructive actions; the same action looks the
  same everywhere; primary button text states the action, not "OK"/"Submit".
- **Spacing & alignment.** Consistent spacing scale and alignment; fields, labels, and helper text line
  up; no ad-hoc one-off margins that break the rhythm.
- **Pills / badges / chips.** Status pills use a consistent shape, size, and color mapping (e.g. one
  green for "active" everywhere); a status never renders as a raw enum string in a pill.
- **Components reused, not forked.** Extend the existing design-system components/tokens rather than
  introducing a parallel divergent style for the same need.

---

## How "done" is judged

A user-facing screen MUST NOT be marked done or PASS unless, for the surface changed:

1. Every applicable FORMS item is satisfied (or an explicit, justified exception is recorded in the
   PR/issue) — confirm-password, show/hide, strength, on-blur + inline validation, specific errors,
   disabled+spinner submit, autocomplete, autofocus, Enter-to-submit, paste allowed.
2. AUTH affordances are present and honest — forgot/reset path or an honest contact note,
   session/remember-me matches real behavior, sign-in⇄sign-up switch is unambiguous.
3. ACCESSIBILITY floor passes — labelled inputs, keyboard operable, visible focus, AA contrast
   (measured), ARIA where needed, errors linked to fields — checked with a tool/audit, not eyeballed.
4. Empty, loading, and error STATES are designed AND observed rendering for every data view; recovery
   from error is demonstrated.
5. MICROCOPY is plain and specific with zero raw enum/JSON/placeholder reaching the user.
6. CONSISTENCY holds for buttons, spacing, and pills against the existing design system.

Any unchecked item is either fixed before sign-off or filed as a blocking defect and flagged to the
owning role — never left silently incomplete. A green render is evidence the screen mounts; this
checklist is the evidence a person can actually use it.
