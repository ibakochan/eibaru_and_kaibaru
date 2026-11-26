"""Exact-string English for pages that render a Japanese message.

Japanese mode returns the original string. English mode replaces it only
when the whole string is one of the known system messages.
"""


_PAGE_EN = {
    "このリンクは無効です。": "This link is not valid.",
    "この予約は見つからないか、リンクが無効です。": "We couldn't find this booking, or the link is not valid.",
    "この予約は見つかりません。": "We couldn't find this booking.",
    "確定した予約のみキャンセルできます。": "Only a confirmed booking can be cancelled.",
    "この予約はすでにキャンセルされています。": "This booking is already cancelled.",
    "この予約はすでに終了しているため、キャンセルできません。": "This booking has already finished, so it can't be cancelled.",
    "この予約はキャンセルされていません。": "This booking is not cancelled.",
    "定員に達しているため、キャンセルを取り消せません。": "This is full, so the cancellation can't be undone.",
    "この時間に空いているワーカーがいません。": "No one is free at that time.",
    "欠席として記録された予約はキャンセルできません。ご不明な点はクラブにお問い合わせください。": "A booking marked as an absence can't be cancelled. Contact the club if you have a question.",
    "お支払い済みの予約はキャンセルできません。ご不明な点はクラブにお問い合わせください。": "A booking that's already been paid can't be cancelled. Contact the club if you have a question.",
    "この人はすでに大会の組み合わせに入っています。先にチーム、ブラケット、種目、または対戦から外してください。": "This person is already placed in the competition. Remove them from the team, bracket, division, or match first.",
    "すでに大会の組み合わせに入っています。キャンセルするには、大会の担当者に連絡してください。": "You're already placed in the competition. Contact the organizer if you need to cancel.",
    "この予約日はすでに過ぎています。もう一度お申し込みください。": "That date has already passed. Please book again.",
    "現在、体験予約を受け付けていません。": "Intro classes aren't open for booking right now.",
    "現在、会員予約を受け付けていません。": "Member bookings aren't open right now.",
    "現在、ビジター予約を受け付けていません。": "Drop-in bookings aren't open right now.",
    "このリンクの有効期限が切れています。もう一度お申し込みください。": "This link has expired. Please book again.",
    "このイベントは中止になりました。": "This event was cancelled.",
    "このイベントの予約受付は終了しています。": "Booking for this event has closed.",
    "このイベントは一般予約を受け付けていません。": "This event isn't open to drop-in bookings.",
    "このテストは凍結されているため、予約できません。": "This test is locked, so it can't be booked.",
    "この競技は凍結されているため、予約できません。": "This division is locked, so it can't be booked.",
    "この対戦は凍結されているため、予約できません。": "This match is locked, so it can't be booked.",
    "このトーナメントは凍結されているため、予約できません。": "This bracket is locked, so it can't be booked.",
    "この団体戦は凍結されているため、予約できません。": "This team match is locked, so it can't be booked.",
    "この団体トーナメントは凍結されているため、予約できません。": "This team bracket is locked, so it can't be booked.",
    "予約の受付は終了しています。": "Booking has closed.",
    "決済ページを作成できませんでした。しばらくしてからもう一度お試しください。": "The payment page couldn't be created. Please try again in a little while.",
    "決済の確認ができませんでした。しばらくしてからもう一度お試しください。": "The payment couldn't be confirmed. Please try again in a little while.",
    "予約内容を確認できませんでした。": "The booking couldn't be confirmed.",
    "この予約の日時はすでに過ぎています。": "That appointment time has already passed.",
    "この予約の開始時刻はすでに過ぎています。": "That start time has already passed.",
    "この依頼は見つからないか、すでに完了しています。": "This request can't be found, or it's already finished.",
    "この依頼は見つかりません。": "This request can't be found.",
    "この依頼はすでに完了しているか、取り消されています。": "This request is already finished or has been cancelled.",
    "この会員はもう移せません。": "This member can't be moved anymore.",
    "許可なし": "You don't have permission to do that.",
    "アカウントに紐づいている会員は移せません。": "A member who already has their own login can't be moved.",
    "このメールアドレスのアカウントが見つかりません。": "No account uses that email address.",
    "自分のアカウントには移せません。": "You can't move a member to your own account.",
    "移管の依頼を送信しました。": "The transfer request was sent.",
    "同じ名前と生年月日の会員が、すでにあなたのアカウントにいます。": "Your account already has a member with the same name and date of birth.",
    "受け取るアカウントに、同じ会員の同じプランがすでにあります。": "The receiving account already has this member on that plan.",
}


def shown(english, text):
    if not english or not isinstance(text, str) or not text:
        return text
    if text in _PAGE_EN:
        return _PAGE_EN[text]
    from .message_en import MESSAGE_EN

    return MESSAGE_EN.get(text, text)
