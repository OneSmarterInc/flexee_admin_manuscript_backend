from django.db import migrations


FIELD_NOTES_CONFIG = {
    'aims_scope': (
        'Field Notes Journal publishes first-hand practitioner and faculty accounts of how a business '
        'actually used AI, including what was tried, what happened, and what did not work.'
    ),
    'article_types': ['Practitioner article'],
    'accepted_methods': [],
    'quality_threshold': (
        'A strong article is a grounded account of a real business use of AI and substantively covers '
        'the business/problem, what was tried, what happened, and what did not work.'
    ),
    'reviewer_criteria': [],
    'policies': {
        'word_count': {'min': 1500, 'max': 3000},
        'authorship': 'Human-authored with AI assistance; AI-authored articles are not accepted.',
        'coauthorship': 'Encouraged',
        'publication': 'Published online on a rolling basis and free to read.',
        'first_review_days': 15,
    },
    'disclosures': [
        'Specific AI-use disclosure required, including drafting, editing, research, or other assistance.'
    ],
    'reporting_standards': [
        'Describe the business and the problem it faced.',
        'Describe what was tried.',
        'Describe what happened.',
        'Describe what did not work.',
    ],
    'desk_rejection_rules': [
        'A required four-part element is missing, including what did not work.',
        'The manuscript is vendor marketing, an abstract think-piece, or has no real business behind it.',
        'The required AI-use disclosure is absent.',
    ],
    'deadlines': {'submission': 'rolling', 'first_review_days': 15},
    'submission_capacity': {},
    'current_demand': {},
    'config_notes': 'Seeded from the existing Field Notes Journal submission page and deterministic review criteria.',
}


FIVE_ZERO_CONFIG = {
    'aims_scope': (
        'Five Zero Books publishes companion books paired with one specific Flexee simulation and written '
        'in the Five Zero format. The manuscript should teach the decisions, roles, and mechanics presented '
        'by the named simulation rather than function as a generic textbook.'
    ),
    'article_types': ['Book manuscript'],
    'accepted_methods': [],
    'quality_threshold': (
        'The manuscript must genuinely pair with the named Flexee simulation, deliver the learning promised '
        'by its introduction and table of contents, and meet the structural Five Zero requirements.'
    ),
    'reviewer_criteria': [],
    'policies': {
        'chapters_required': 12,
        'total_words': {'min': 25000, 'max': 30000},
        'opening_chapter_words': {'min': 1000, 'max': 1900},
        'body_chapter_ideal_words': {'min': 2100, 'max': 2550},
        'body_chapter_hard_words': {'min': 1800, 'max': 2800},
        'figures_required': 40,
        'authorship': 'Human-authored with AI assistance; AI-authored manuscripts are not accepted.',
        'first_review_days': 15,
        'submission': 'rolling',
        'supported_simulations': [
            'Flexee Supply Chain',
            'Flexee Supply Chain Executive',
            'Flexee Data Analytics',
            'Flexee Healthcare',
            'Flexee Financial Accounting',
            'Flexee ERP',
            'Flexee Defense Acquisitions Specialist',
            'Flexee MIS',
            'Flexee Systems Analysis and Design',
            'Flexee Management Accounting',
            'Flexee Corporate Strategy',
            'Flexee Marketing Strategy',
            'Flexee Managerial Economics',
            'Flexee Project Management',
            'Flexee Business Process Management',
            'Flexee Cybersecurity',
            'Flexee Nursing Leadership',
            'Flexee AI Decision Economics',
        ],
    },
    'disclosures': [
        'Specific AI-use disclosure required, including drafting, figure generation, editing, research, or other assistance.'
    ],
    'reporting_standards': [
        'Pair the book with one specific Flexee simulation.',
        'Use clear chapter headings for the twelve-chapter structure.',
        'Caption figures as Figure 1, Figure 2, and so on so structural checks can read them.',
    ],
    'desk_rejection_rules': [
        'Required structural checks fail.',
        'The manuscript is a generic treatment that would read the same with the named simulation removed.',
        'The manuscript does not teach what its front matter says it will.',
        'The required AI-use disclosure is absent.',
    ],
    'deadlines': {'submission': 'rolling', 'first_review_days': 15},
    'submission_capacity': {},
    'current_demand': {},
    'config_notes': 'Seeded from the existing Five Zero Books submission page and deterministic review criteria.',
}


def seed_flexee_venues(apps, schema_editor):
    Organization = apps.get_model('review', 'Organization')
    Venue = apps.get_model('review', 'Venue')
    VenueAgentConfig = apps.get_model('review', 'VenueAgentConfig')

    organization, _ = Organization.objects.get_or_create(
        name='Flexee Publishing',
        defaults={'organization_type': 'publisher', 'active': True},
    )

    seeds = [
        {
            'slug': 'field-notes-journal',
            'name': 'Field Notes Journal',
            'venue_type': 'journal',
            'description': (
                'Practitioner accounts of how a business actually used AI, including the parts that did not work.'
            ),
            'config': FIELD_NOTES_CONFIG,
        },
        {
            'slug': 'five-zero-books',
            'name': 'Five Zero Books',
            'venue_type': 'publisher',
            'description': (
                'Companion books paired with one Flexee simulation and written in the Five Zero format.'
            ),
            'config': FIVE_ZERO_CONFIG,
        },
    ]

    for seed in seeds:
        venue, _ = Venue.objects.get_or_create(
            slug=seed['slug'],
            defaults={
                'organization': organization,
                'name': seed['name'],
                'venue_type': seed['venue_type'],
                'description': seed['description'],
                'active': True,
            },
        )
        if not VenueAgentConfig.objects.filter(venue=venue).exists():
            VenueAgentConfig.objects.create(
                venue=venue,
                version=1,
                active=True,
                **seed['config'],
            )


def no_reverse(apps, schema_editor):
    # Do not delete editorial data or venue records on reverse migration.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('review', '0009_manuscript_access_token_hash'),
    ]

    operations = [
        migrations.RunPython(seed_flexee_venues, no_reverse),
    ]
