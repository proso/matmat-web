from math import sqrt
from random import random
from elo.model import EloModel
from model.models import UserSkill, Skill
from questions.models import Question


def recommend_questions(count, user, skills, in_queue):
    questions = Question.objects.raw(
        "SELECT questions_question.*, "
        "COUNT(questions_answer.id) AS answers_count, "
        "MIN(TIMESTAMPDIFF(SECOND, questions_answer.timestamp,  NOW())) AS time_form_last_answer,"
        "model_questiondifficulty.value as questiondifficulty "
        "FROM questions_question "
        "LEFT JOIN model_userskill ON ( model_userskill.skill_id = questions_question.skill_id AND model_userskill.user_id = %(user_id)s)"
        "LEFT JOIN questions_answer ON ( questions_question.id = questions_answer.question_id AND questions_answer.user_id = %(user_id)s) "
        "LEFT JOIN model_questiondifficulty ON ( model_questiondifficulty.question_id = questions_question.id) "
        "WHERE questions_question.skill_id IN ( {0} ) {1} "
        "GROUP BY questions_question.id ".format(
            skills,
            "AND questions_question.id NOT IN ( {0} ) ".format(",".join(in_queue)) if in_queue else ""
        ),
        {"user_id": user.pk,}
    )

    skill_pks = set()
    skill_tree = {}
    for s in Skill.objects.filter(pk__in=skills.split(",")).select_related("parent"):
        while s is not None and s.pk not in skill_pks:
            skill_pks.add(s.pk)
            s.user_skill = None
            skill_tree[s.pk] = s

            s = s.parent

    for us in UserSkill.objects.filter(user=user, skill__in=skill_pks):
        skill_tree[us.skill_id].user_skill = us.value
    similar_questions_times = get_similar_questions_times(user, in_queue)

    questions = list(questions)

    selected = []
    for i in range(count):
        questions.sort(key=lambda q: question_priority(q, user.is_staff, skill_tree, similar_questions_times), reverse=True)
        selected.append(questions[0])
        similar_questions_times[questions[0].value] = 0
        questions = questions[1:]

    return selected


def get_similar_questions_times(user, in_queue):
    """
    compute times in seconds from last answer to similar (with same value) question
    """
    questions = Question.objects.raw(
        "SELECT "
           "questions_question.id, "
           "TIMESTAMPDIFF(SECOND, MAX(questions_answer.timestamp), NOW()) AS time_form_last_answer, "
           "questions_question.value "
        "FROM "
           "questions_question "
        "LEFT JOIN questions_answer ON ( questions_question.id = questions_answer.question_id AND questions_answer.user_id = %(user_id)s) "
        "GROUP BY questions_question.value ",
        {"user_id": user.pk}
    )

    times = {}
    for q in questions:
        if q.time_form_last_answer is not None:
            times[q.value] = q.time_form_last_answer

    for q in Question.objects.filter(pk__in=in_queue):
        times[q.value] = 0

    return times

def question_priority(question, log, skill_tree, similar_questions_times):
    GOAL_RESPONSE = 0.7             # targeted probability of success
    ESTIMATE_WEIGHT = 10            # bonus for suitable difficult questions
    TIME_WEIGHT = 120               # seconds after which penalty for question is 1
    TIME_SIMILAR_WEIGHT = 30        # seconds after which penalty for similar question is 1
    COUNT_WEIGHT = 2                # bonus for less answered questions
    RANDOM_WEIGHT = 0.001           # random bonus from [0, 1]

    # answer count score
    count_score = 1. / (sqrt(1 + question.answers_count))

    # time from last answer to same question score
    if question.time_form_last_answer is None:
        time_score = 0
    else:
        time_score = -1. / (question.time_form_last_answer) if question.time_form_last_answer > 0 else -1

    # time from last answer to similar question score
    if question.value not in similar_questions_times.keys():
        time_similar_score = 0
        time = None
    else:
        time = similar_questions_times[question.value]
        time_similar_score = -1. / (time) if time > 0 else -1

    # compute user skill
    user_skill = None
    current_skill_pk = question.skill_id
    while user_skill is None:
        if skill_tree[current_skill_pk].parent is None:
            user_skill = 0
            break
        user_skill = skill_tree[current_skill_pk].user_skill
        current_skill_pk = skill_tree[current_skill_pk].parent.pk

    # difficulty score
    difficulty = question.questiondifficulty if question.questiondifficulty is not None else 0
    expected_response = EloModel.expected_response(user_skill, difficulty, question.type)
    if GOAL_RESPONSE > expected_response:
        estimate_score = expected_response / GOAL_RESPONSE
    else:
        estimate_score = (1 - expected_response) / (1 - GOAL_RESPONSE)

    priority = count_score * COUNT_WEIGHT + time_score * TIME_WEIGHT + time_similar_score * TIME_SIMILAR_WEIGHT+ estimate_score * ESTIMATE_WEIGHT + RANDOM_WEIGHT * random()

    if log:
        question.recommendation_log = {
            "count_score": "{0} * {1} = {2}".format(count_score, COUNT_WEIGHT, count_score * COUNT_WEIGHT),
            "time_score": "-{1} / {0}  = {2}".format(question.time_form_last_answer, TIME_WEIGHT, time_score * TIME_WEIGHT),
            "time_similar_score": "-{1} / {0}  = {2}".format(time, TIME_SIMILAR_WEIGHT, time_similar_score * TIME_SIMILAR_WEIGHT),
            "estimate_score": "{0} * {1} = {2}".format(estimate_score, ESTIMATE_WEIGHT, estimate_score * ESTIMATE_WEIGHT),
            "expected_response": expected_response,
            "total": priority,
            "user_skill": user_skill,
            "difficulty": difficulty,
        }

    return priority
