--
-- macos_tools.applescript - NevNew's local macOS tools (issue #2)
-- Invoked by the n8n "NevNew macOS + GitHub Tools" workflow's toolCode nodes:
--
--   osascript /Users/grit/nevnew/n8n/macos_tools.applescript <subcommand> [args...]
--
-- Subcommands:
--   list_reminders                     : reminders due within 7 days (incl. overdue)
--   create_reminder <title> <dueISO>   : create reminder; dueISO "YYYY-MM-DD HH:MM" or "-" for none
--   create_event <title> <start> <end> : calendar event, first writable calendar
--   list_events <dayISO>               : events on YYYY-MM-DD
--   append_note <noteName> <body>      : append text to note (create if missing)
--
-- Args arrive via "on run argv" (JSON.stringify'd on the toolCode side, so
-- quoting is POSIX-safe). Text output only. NOTE: keep this comment free of
-- em dashes and bare pipe characters - osacompile mis-lexes both inside
-- comments (empirically verified 2026-09-06).
--

on run argv
	if (count of argv) is 0 then return "ERROR: no subcommand"
	set cmd to item 1 of argv
	if cmd is "list_reminders" then
		return my list_reminders()
	else if cmd is "create_reminder" then
		if (count of argv) < 3 then return "ERROR: create_reminder needs <title> <dueISO|->"
		return my create_reminder(item 2 of argv, item 3 of argv)
	else if cmd is "create_event" then
		if (count of argv) < 4 then return "ERROR: create_event needs <title> <start> <end>"
		return my create_event(item 2 of argv, item 3 of argv, item 4 of argv)
	else if cmd is "list_events" then
		if (count of argv) < 2 then return "ERROR: list_events needs <dayISO YYYY-MM-DD>"
		return my list_events(item 2 of argv)
	else if cmd is "append_note" then
		if (count of argv) < 3 then return "ERROR: append_note needs <noteName> <body>"
		return my append_note(item 2 of argv, item 3 of argv)
	else
		return "ERROR: unknown subcommand: " & cmd
	end if
end run

-- Parse "YYYY-MM-DD HH:MM" (time optional) into a date. missing value on garbage.
on parse_iso(iso)
	try
		if (length of iso) < 10 then return missing value
		set y to text 1 thru 4 of iso as integer
		set mo to text 6 thru 7 of iso as integer
		set d to text 9 thru 10 of iso as integer
		set hh to 0
		set mm to 0
		if (length of iso) ≥ 16 then
			set hh to text 12 thru 13 of iso as integer
			set mm to text 15 thru 16 of iso as integer
		end if
		if mo < 1 or mo > 12 or d < 1 or d > 31 then return missing value
		if hh > 23 or mm > 59 then return missing value
		set dt to current date
		set seconds of dt to 0
		set hours of dt to hh
		set minutes of dt to mm
		set day of dt to 1
		set month of dt to mo
		set day of dt to d
		set year of dt to y
		return dt
	on error
		return missing value
	end try
end parse_iso

on fmt(dt)
	set p to (month of dt as integer) as text
	if (length of p) < 2 then set p to "0" & p
	set q to (day of dt) as text
	if (length of q) < 2 then set q to "0" & q
	set r to (hours of dt) as text
	if (length of r) < 2 then set r to "0" & r
	set s to (minutes of dt) as text
	if (length of s) < 2 then set s to "0" & s
	return (year of dt) & "-" & p & "-" & q & " " & r & ":" & s
end fmt

on time_only(dt)
	set r to (hours of dt) as text
	if (length of r) < 2 then set r to "0" & r
	set s to (minutes of dt) as text
	if (length of s) < 2 then set s to "0" & s
	return r & ":" & s
end time_only

on esc_html(t)
	set t to my replace(t, "&", "&amp;")
	set t to my replace(t, "<", "&lt;")
	set t to my replace(t, ">", "&gt;")
	return t
end esc_html

on replace(t, fromT, toT)
	set AppleScript's text item delimiters to fromT
	set parts to text items of t
	set AppleScript's text item delimiters to toT
	set t to parts as text
	set AppleScript's text item delimiters to ""
	return t
end replace

on list_reminders()
	set now to current date
	set horizon to now + 7 * days
	set earliest to now - 1 * days
	set out to ""
	try
		tell application "Reminders"
			set dueRems to (reminders whose completed is false and due date ≤ horizon and due date ≥ earliest)
			repeat with r in dueRems
				set out to out & "• " & (my fmt(due date of r)) & " — " & (name of r) & linefeed
			end repeat
		end tell
	on error errm
		return "ERROR (Reminders — check automation permission): " & errm
	end try
	if out is "" then return "No reminders due in the next 7 days."
	return out
end list_reminders

on create_reminder(remName, dueISO)
	if remName is "-" or remName is "" then return "ERROR: empty title"
	set dueDate to missing value
	if dueISO is not "-" and dueISO is not "" then
		set dueDate to my parse_iso(dueISO)
		if dueDate is missing value then return "ERROR: bad due date (want YYYY-MM-DD HH:MM): " & dueISO
	end if
	try
		tell application "Reminders"
			if dueDate is missing value then
				make new reminder with properties {name:remName}
			else
				make new reminder with properties {name:remName, due date:dueDate}
			end if
		end tell
	on error errm
		return "ERROR (Reminders — check automation permission): " & errm
	end try
	if dueDate is missing value then
		return "Created reminder: " & remName
	end if
	return "Created reminder: " & remName & " (due " & my fmt(dueDate) & ")"
end create_reminder

on create_event(evTitle, startISO, endISO)
	if evTitle is "-" or evTitle is "" then return "ERROR: empty title"
	set sDate to my parse_iso(startISO)
	set eDate to my parse_iso(endISO)
	if sDate is missing value or eDate is missing value then return "ERROR: bad start/end (want YYYY-MM-DD HH:MM)"
	if eDate ≤ sDate then return "ERROR: end must be after start"
	try
		tell application "Calendar"
			set targetCal to missing value
			repeat with c in calendars
				if writable of c is true then
					set targetCal to c
					exit repeat
				end if
			end repeat
			if targetCal is missing value then return "ERROR: no writable calendar found"
			tell targetCal to make new event with properties {summary:evTitle, start date:sDate, end date:eDate}
			return "Created event: " & evTitle & " (" & my fmt(sDate) & " → " & my fmt(eDate) & ") in calendar \"" & (name of targetCal) & "\""
		end tell
	on error errm
		return "ERROR (Calendar — check automation permission): " & errm
	end try
end create_event

on list_events(dayISO)
	set dayStart to my parse_iso(dayISO)
	if dayStart is missing value then return "ERROR: bad day (want YYYY-MM-DD)"
	set dayEnd to dayStart + 1 * days
	set out to ""
	try
		tell application "Calendar"
			repeat with c in calendars
				try
					set evs to (every event of c whose start date ≥ dayStart and start date < dayEnd)
					repeat with ev in evs
						set evStart to start date of ev
						set evEnd to end date of ev
						if (allday event of ev) is true then
							set out to out & "• all-day — " & (summary of ev) & linefeed
						else
							set out to out & "• " & (my time_only(evStart)) & "–" & (my time_only(evEnd)) & " — " & (summary of ev) & linefeed
						end if
					end repeat
				end try
			end repeat
		end tell
	on error errm
		return "ERROR (Calendar — check automation permission): " & errm
	end try
	if out is "" then return "No events on " & dayISO & "."
	return out
end list_events

on append_note(noteName, bodyText)
	if noteName is "-" or noteName is "" then return "ERROR: empty note name"
	if bodyText is "-" or bodyText is "" then return "ERROR: empty body"
	set htmlBody to "<div>" & my esc_html(bodyText) & "</div>"
	try
		tell application "Notes"
			set acct to default account
			set matches to (notes of acct whose name is noteName)
			if (count of matches) > 0 then
				set n to item 1 of matches
				set body of n to (body of n) & htmlBody
				return "Appended to note: " & noteName
			else
				make new note at folder "Notes" of acct with properties {name:noteName, body:htmlBody}
				return "Created note: " & noteName
			end if
		end tell
	on error errm
		return "ERROR (Notes — check automation permission): " & errm
	end try
end append_note