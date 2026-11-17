#!/bin/sh
# SubagentStart hook: print the hook JSON whose additionalContext is
# "conclave plugin guidance\n" followed by the guidance file given as $1.
[ -r "$1" ] || exit 0
# $(tail -c 1) is empty when the file ends with a newline (command substitution strips it).
[ -z "$(tail -c 1 "$1")" ] && nl=1 || nl=0
awk -v nl="$nl" '
BEGIN {
	# JSON escapes: backslash, double quote, and every control character as \u00XX.
	esc["\\"] = "\\\\"; esc["\""] = "\\\""
	for (i = 1; i < 32; i++) esc[sprintf("%c", i)] = sprintf("\\u%04x", i)
	printf "{\"hookSpecificOutput\":{\"hookEventName\":\"SubagentStart\",\"additionalContext\":\"conclave plugin guidance\\n"
}
{
	if (NR > 1) printf "\\n"
	for (i = 1; i <= length($0); i++) { c = substr($0, i, 1); printf "%s", (c in esc) ? esc[c] : c }
}
END { if (NR && nl) printf "\\n"; print "\"}}" }
' "$1"
