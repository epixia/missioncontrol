"""Pure model-level tests for the new kanban tables — no DB I/O, matching
the pattern of testing SQLModel defaults directly on an instance."""

from mission_control.server.models import Ticket, TicketColumn, TicketComment


def test_ticket_defaults_to_backlog_column_and_position_zero():
    ticket = Ticket(mission_id="m1", title="Do the thing")
    assert ticket.column == TicketColumn.BACKLOG
    assert ticket.position == 0
    assert ticket.description == ""
    assert ticket.created_by_role is None


def test_ticket_table_name_is_lowercase_class_name():
    # Later tasks' foreign_key="ticket.id" strings depend on this exact name,
    # the same convention already used for mission.id/missiontask.id.
    assert Ticket.__tablename__ == "ticket"


def test_ticket_comment_defaults():
    comment = TicketComment(ticket_id="t1", text="looks good")
    assert comment.author_role is None
    assert comment.text == "looks good"
    assert TicketComment.__tablename__ == "ticketcomment"
