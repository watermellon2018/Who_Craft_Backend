"""Transactional private messaging and polling snapshots.

Mutations lock both user rows in ID order, then the conversation. Account
closure already owns its user lock and locks conversations in UUID order,
without acquiring peer user locks. Read snapshots take conversation locks only.
"""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Count, F, OuterRef, Q, Subquery
from django.utils import timezone

from w_craft_back.storage_gateway import signed_url_for_file

from .errors import MessagingError
from .models import (
    MessagingConversation,
    MessagingMessage,
    MessagingParticipant,
)
from .serializers import MessageTextSerializer


def _not_found() -> MessagingError:
    return MessagingError('MESSAGING_NOT_FOUND', 'Conversation not found.', 404)


def _require_active(user: User) -> None:
    if not User.objects.filter(pk=user.pk, is_active=True).exists():
        raise MessagingError('ACCOUNT_INACTIVE', 'Account is inactive.', 401)


def _lock_users(actor: User, peer_id: int) -> dict[int, User]:
    users = {
        user.pk: user
        for user in User.objects.select_for_update()
        .filter(pk__in=sorted({actor.pk, peer_id})).order_by('pk')
    }
    if actor.pk not in users or not users[actor.pk].is_active:
        raise MessagingError('ACCOUNT_INACTIVE', 'Account is inactive.', 401)
    if peer_id not in users:
        raise MessagingError(
            'RECIPIENT_UNAVAILABLE', 'Recipient is unavailable.', 404,
        )
    return users


def _locked_conversation(
    actor: User, conversation_id: UUID, *, mutation: bool = False,
) -> MessagingConversation:
    pair = MessagingConversation.objects.filter(
        Q(user_low=actor) | Q(user_high=actor), pk=conversation_id,
    ).values('user_low_id', 'user_high_id').first()
    if pair is None:
        raise _not_found()
    if mutation:
        peer_id = (
            pair['user_high_id'] if pair['user_low_id'] == actor.pk
            else pair['user_low_id']
        )
        _lock_users(actor, peer_id)
    else:
        _require_active(actor)
    return MessagingConversation.objects.select_for_update().get(
        pk=conversation_id,
    )


def _state(
    conversation: MessagingConversation, actor: User,
) -> MessagingParticipant:
    return MessagingParticipant.objects.get(
        conversation=conversation, user=actor,
    )


def _text(value: str) -> str:
    serializer = MessageTextSerializer(data={'text': value})
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data['text']


def _user_data(user: User, request: Any = None) -> dict:
    profile = getattr(user, 'profile', None) if user.is_active else None
    username = (
        (profile.public_username if profile else None) or user.username
    ) if user.is_active else ''
    return {
        'id': user.pk,
        'username': username,
        'display_name': (profile.display_name or username) if profile else username,
        'avatar_url': signed_url_for_file(profile.avatar, request) if profile else None,
        'is_active': user.is_active,
    }


def _message_data(message: MessagingMessage, actor: User) -> dict:
    return {
        'id': message.pk,
        'conversation_id': str(message.conversation_id),
        'sequence': message.sequence,
        'sender_id': message.sender_id,
        'is_own': message.sender_id == actor.pk,
        'text': '' if message.deleted_at else message.text,
        'created_at': message.created_at.isoformat(),
        'edited_at': message.edited_at.isoformat() if message.edited_at else None,
        'deleted_at': message.deleted_at.isoformat() if message.deleted_at else None,
        'version': message.version,
        'client_id': str(message.client_id),
    }


def _conversation_data_many(
    conversations: list[MessagingConversation], actor: User, request: Any = None,
) -> list[dict]:
    """Build a page using three bounded queries, not one query per message."""
    if not conversations:
        return []
    ids = [conversation.pk for conversation in conversations]
    visible = MessagingMessage.objects.filter(
        conversation_id=OuterRef('conversation_id'),
        sequence__gt=OuterRef('cleared_through'),
        deleted_at__isnull=True,
    )
    unread = (
        visible.filter(sequence__gt=OuterRef('read_through'))
        .exclude(sender=actor).order_by().values('conversation_id')
        .annotate(total=Count('pk')).values('total')
    )
    actors = {
        state.conversation_id: state
        for state in MessagingParticipant.objects.filter(
            conversation_id__in=ids, user=actor,
        ).annotate(
            last_id=Subquery(visible.order_by('-sequence').values('pk')[:1]),
            unread=Subquery(unread),
        )
    }
    peers = {
        state.conversation_id: state
        for state in MessagingParticipant.objects.filter(
            conversation_id__in=ids,
        ).exclude(user=actor).select_related('user__profile')
    }
    last_messages = MessagingMessage.objects.in_bulk(
        [state.last_id for state in actors.values() if state.last_id is not None],
    )
    result = []
    for conversation in conversations:
        own = actors[conversation.pk]
        peer = peers[conversation.pk]
        last = last_messages.get(own.last_id)
        result.append({
            'id': str(conversation.pk),
            'peer': _user_data(peer.user, request),
            'last_message': _message_data(last, actor) if last else None,
            'unread_count': 0 if own.is_hidden else (own.unread or 0),
            'last_sequence': conversation.last_sequence,
            'revision': conversation.revision,
            'read_through': own.read_through,
            'peer_read_through': peer.read_through,
            'cleared_through': own.cleared_through,
            'is_hidden': own.is_hidden,
            'can_send': peer.user.is_active,
            'updated_at': conversation.updated_at.isoformat(),
        })
    return result


def _conversation_data(
    conversation: MessagingConversation, actor: User, request: Any = None,
) -> dict:
    return _conversation_data_many([conversation], actor, request)[0]


def _message_result(
    message: MessagingMessage, conversation: MessagingConversation,
    actor: User, request: Any = None,
) -> dict:
    return {
        'message': _message_data(message, actor),
        'conversation': _conversation_data(conversation, actor, request),
    }


def search_users(
    actor: User, q: str = '', limit: int = 20, offset: int = 0,
    request: Any = None,
) -> dict:
    """Search active users, including registrations without a profile row."""
    _require_active(actor)
    q = q.strip().removeprefix('@').strip()
    users = User.objects.none()
    if 2 <= len(q) <= 100:
        users = User.objects.filter(
            Q(username__icontains=q) | Q(profile__public_username__icontains=q)
            | Q(profile__display_name__icontains=q),
            is_active=True,
        ).exclude(pk=actor.pk).select_related('profile').order_by('username', 'pk')
    return {
        'items': [_user_data(user, request) for user in users[offset:offset + limit]],
        'total': users.count(), 'limit': limit, 'offset': offset,
    }


def unread_count(actor: User) -> int:
    """Count visible, nondeleted incoming messages above both personal cursors."""
    _require_active(actor)
    return MessagingMessage.objects.filter(
        Q(sequence__gt=F('conversation__participants__read_through'))
        & Q(sequence__gt=F('conversation__participants__cleared_through')),
        conversation__participants__user=actor,
        conversation__participants__is_hidden=False,
        deleted_at__isnull=True,
    ).exclude(sender=actor).count()


@transaction.atomic
def list_conversations(
    actor: User, limit: int = 30, offset: int = 0, request: Any = None,
) -> dict:
    """List recent visible conversations; always acquire locks in UUID order."""
    _require_active(actor)
    visible = MessagingConversation.objects.filter(
        participants__user=actor, participants__is_hidden=False,
    )
    ids = list(visible.order_by('-updated_at', '-pk').values_list(
        'pk', flat=True,
    )[offset:offset + limit])
    conversations = list(
        MessagingConversation.objects.select_for_update()
        .filter(pk__in=ids).order_by('pk')
    )
    conversations.sort(key=lambda row: (row.updated_at, row.pk), reverse=True)
    items = _conversation_data_many(conversations, actor, request)
    return {
        'items': [item for item in items if not item['is_hidden']],
        'total': visible.count(), 'unread_count': unread_count(actor),
        'limit': limit, 'offset': offset,
    }


@transaction.atomic
def open_conversation(
    actor: User, recipient_id: int, request: Any = None,
) -> dict:
    """Create the pair once; retries never restore personally cleared history."""
    if recipient_id == actor.pk:
        raise MessagingError('SELF_MESSAGING', 'Cannot message yourself.')
    users = _lock_users(actor, recipient_id)
    if not users[recipient_id].is_active:
        raise MessagingError(
            'RECIPIENT_UNAVAILABLE', 'Recipient is unavailable.', 404,
        )
    low, high = sorted(users)
    conversation, created = MessagingConversation.objects.get_or_create(
        user_low_id=low, user_high_id=high,
    )
    if created:
        MessagingParticipant.objects.bulk_create([
            MessagingParticipant(conversation=conversation, user_id=user_id,
                                 is_hidden=user_id != actor.pk)
            for user_id in (low, high)
        ])
    else:
        conversation = MessagingConversation.objects.select_for_update().get(
            pk=conversation.pk,
        )
    return _conversation_data(conversation, actor, request)


@transaction.atomic
def get_conversation(
    actor: User, conversation_id: UUID, request: Any = None,
) -> dict:
    """Return hidden state too, so other tabs can reconcile a personal clear."""
    conversation = _locked_conversation(actor, conversation_id)
    return _conversation_data(conversation, actor, request)


@transaction.atomic
def clear_conversation(
    actor: User, conversation_id: UUID, request: Any = None,
) -> dict:
    """Hide this user's history without claiming they read the incoming text."""
    conversation = _locked_conversation(actor, conversation_id, mutation=True)
    state = _state(conversation, actor)
    if not state.is_hidden or state.cleared_through != conversation.last_sequence:
        state.is_hidden = True
        state.cleared_through = conversation.last_sequence
        state.save(update_fields=['is_hidden', 'cleared_through'])
        conversation.revision += 1
        conversation.save(update_fields=['revision'])
    return _conversation_data(conversation, actor, request)


@transaction.atomic
def message_history(
    actor: User, conversation_id: UUID, before: int | None = None,
    limit: int = 50, request: Any = None,
) -> dict:
    """Read history and its sync cursor from the same locked snapshot."""
    conversation = _locked_conversation(actor, conversation_id)
    state = _state(conversation, actor)
    messages = conversation.messages.filter(sequence__gt=state.cleared_through)
    if before is not None:
        messages = messages.filter(sequence__lt=before)
    rows = list(messages.order_by('-sequence')[:limit + 1])
    has_more = len(rows) > limit
    rows = list(reversed(rows[:limit]))
    return {
        'items': [_message_data(row, actor) for row in rows],
        'next_before': rows[0].sequence if has_more else None,
        'sync_revision': conversation.revision,
        'conversation': _conversation_data(conversation, actor, request),
    }


@transaction.atomic
def send_message(
    actor: User, conversation_id: UUID, text: str, client_id: UUID,
    request: Any = None,
) -> dict:
    """Allocate one sequence per new send and retain idempotency after edits."""
    text = _text(text)
    fingerprint = hashlib.sha256(text.encode('utf-8')).hexdigest()
    conversation = _locked_conversation(actor, conversation_id, mutation=True)
    state = _state(conversation, actor)
    existing = conversation.messages.filter(sender=actor, client_id=client_id).first()
    if existing is not None:
        if existing.send_fingerprint != fingerprint:
            raise MessagingError(
                'CLIENT_ID_CONFLICT', 'Client ID was used for different text.', 409,
            )
        if existing.sequence <= state.cleared_through:
            raise MessagingError(
                'MESSAGE_CLEARED', 'This message belongs to cleared history.', 409,
            )
        return _message_result(existing, conversation, actor, request)
    peer_id = (
        conversation.user_high_id if conversation.user_low_id == actor.pk
        else conversation.user_low_id
    )
    if not User.objects.filter(pk=peer_id, is_active=True).exists():
        raise MessagingError(
            'RECIPIENT_UNAVAILABLE', 'Recipient is unavailable.', 409,
        )
    conversation.last_sequence += 1
    conversation.revision += 1
    conversation.updated_at = timezone.now()
    conversation.save(update_fields=['last_sequence', 'revision', 'updated_at'])
    message = MessagingMessage.objects.create(
        conversation=conversation, sender=actor, text=text, client_id=client_id,
        send_fingerprint=fingerprint, sequence=conversation.last_sequence,
        version=conversation.revision,
    )
    conversation.participants.update(is_hidden=False)
    return _message_result(message, conversation, actor, request)


def _authored_message(
    conversation: MessagingConversation, actor: User, message_id: int,
) -> MessagingMessage:
    state = _state(conversation, actor)
    message = conversation.messages.filter(
        pk=message_id, sequence__gt=state.cleared_through,
    ).first()
    if message is None:
        raise MessagingError('MESSAGE_NOT_FOUND', 'Message not found.', 404)
    if message.sender_id != actor.pk:
        raise MessagingError(
            'MESSAGE_NOT_OWNED', 'Only the author can change this message.', 403,
        )
    return message


@transaction.atomic
def edit_message(
    actor: User, conversation_id: UUID, message_id: int, text: str,
    expected_version: int, request: Any = None,
) -> dict:
    """Reject conflicting edits, including a stale edit after message deletion."""
    text = _text(text)
    conversation = _locked_conversation(actor, conversation_id, mutation=True)
    message = _authored_message(conversation, actor, message_id)
    if message.deleted_at is not None:
        raise MessagingError('MESSAGE_DELETED', 'Message was deleted.', 409)
    if message.version != expected_version:
        raise MessagingError('MESSAGE_CONFLICT', 'Message changed. Reload it.', 409)
    if message.text != text:
        conversation.revision += 1
        conversation.save(update_fields=['revision'])
        message.text = text
        message.edited_at = timezone.now()
        message.version = conversation.revision
        message.save(update_fields=['text', 'edited_at', 'version'])
    return _message_result(message, conversation, actor, request)


@transaction.atomic
def delete_message(
    actor: User, conversation_id: UUID, message_id: int, request: Any = None,
) -> dict:
    """Erase authored text for both participants and return a stable tombstone."""
    conversation = _locked_conversation(actor, conversation_id, mutation=True)
    message = _authored_message(conversation, actor, message_id)
    if message.deleted_at is None:
        conversation.revision += 1
        conversation.save(update_fields=['revision'])
        message.text = ''
        message.deleted_at = timezone.now()
        message.version = conversation.revision
        message.save(update_fields=['text', 'deleted_at', 'version'])
    return _message_result(message, conversation, actor, request)


@transaction.atomic
def message_changes(
    actor: User, conversation_id: UUID, after_revision: int = 0,
    limit: int = 100, request: Any = None,
) -> dict:
    """Return latest message states in distinct revision order, with tombstones."""
    conversation = _locked_conversation(actor, conversation_id)
    if after_revision > conversation.revision:
        raise MessagingError('SYNC_CURSOR_INVALID', 'Sync cursor is ahead of history.')
    state = _state(conversation, actor)
    rows = list(conversation.messages.filter(
        sequence__gt=state.cleared_through, version__gt=after_revision,
    ).order_by('version')[:limit + 1])
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {
        'items': [_message_data(row, actor) for row in rows],
        'next_revision': rows[-1].version if has_more else conversation.revision,
        'has_more': has_more,
        'conversation': _conversation_data(conversation, actor, request),
    }


@transaction.atomic
def mark_read(
    actor: User, conversation_id: UUID, through_sequence: int,
    request: Any = None,
) -> dict:
    """Advance receipts only through existing messages in the actor's history."""
    conversation = _locked_conversation(actor, conversation_id, mutation=True)
    state = _state(conversation, actor)
    if through_sequence > state.read_through:
        exists = conversation.messages.filter(sequence=through_sequence).exists()
        if through_sequence <= state.cleared_through or not exists:
            raise MessagingError(
                'READ_BOUNDARY_INVALID', 'Read boundary is outside visible history.',
            )
        state.read_through = through_sequence
        state.save(update_fields=['read_through'])
        conversation.revision += 1
        conversation.save(update_fields=['revision'])
    return _conversation_data(conversation, actor, request)


@transaction.atomic
def scrub_user_messages(user: User) -> None:
    """Erase closing-account text while preserving peers' authored history.

    The caller must hold this user's row lock and mark them inactive first.
    Never acquire peer locks here: another closure may own them already.
    """
    conversations = MessagingConversation.objects.select_for_update().filter(
        Q(user_low=user) | Q(user_high=user),
    ).order_by('pk')
    now = timezone.now()
    for conversation in conversations:
        for message in conversation.messages.filter(
            sender=user, deleted_at__isnull=True,
        ).order_by('sequence'):
            conversation.revision += 1
            message.text = ''
            message.deleted_at = now
            message.version = conversation.revision
            message.save(update_fields=['text', 'deleted_at', 'version'])
        conversation.participants.filter(user=user).update(
            is_hidden=True, cleared_through=conversation.last_sequence,
        )
        # Even an empty conversation changes when its peer becomes unavailable.
        conversation.revision += 1
        conversation.save(update_fields=['revision'])
