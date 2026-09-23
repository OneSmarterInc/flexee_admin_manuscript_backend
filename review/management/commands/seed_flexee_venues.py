from django.core.management.base import BaseCommand
from django.db import transaction

from review.models import Organization, Venue, VenueAgentConfig


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
    'reviewer_criteria': [
        'Applied AI implementation in real organizations',
        'Business operations and change management',
        'Practitioner evidence and implementation outcomes',
    ],
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
    'current_demand': {
        'topics': [
            'How businesses are actually using AI',
            'Measured implementation outcomes',
            'Failed or partially successful AI rollouts',
        ],
        'priority': 'Grounded accounts with concrete outcomes, including what did not work.',
    },
    'config_notes': 'Seeded from the current Field Notes Journal submission description and deterministic review criteria.',
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
    'reviewer_criteria': [
        'Subject-matter expertise for the paired Flexee simulation',
        'Simulation-based learning and instructional design',
        'Applied teaching materials and visual explanation',
    ],
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
    'current_demand': {
        'tracks': [
            'Supply chain',
            'ERP',
            'Data analytics',
            'Healthcare',
            'Cybersecurity',
            'Other current and in-development Flexee simulations',
        ],
        'priority': 'Companion manuscripts for every Flexee simulation.',
    },
    'config_notes': 'Seeded from the current Five Zero Books submission description and deterministic review criteria.',
}


SEEDS = [
    {
        'slug': 'field-notes-journal',
        'name': 'Field Notes Journal',
        'venue_type': 'journal',
        'description': 'Practitioner accounts of how a business actually used AI, including the parts that did not work.',
        'config': FIELD_NOTES_CONFIG,
    },
    {
        'slug': 'five-zero-books',
        'name': 'Five Zero Books',
        'venue_type': 'publisher',
        'description': 'Companion books paired with one Flexee simulation and written in the Five Zero format.',
        'config': FIVE_ZERO_CONFIG,
    },
]


class Command(BaseCommand):
    help = (
        'Create the two initial Flexee Publishing venues and their current venue-agent configuration. '
        'Existing venue configurations are preserved unless --refresh is supplied.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--refresh',
            action='store_true',
            help='Create a new active configuration version from the canonical current submission criteria.',
        )

    @transaction.atomic
    def handle(self, *args, **options):
        organization, _ = Organization.objects.get_or_create(
            name='Flexee Publishing',
            defaults={'organization_type': 'publisher', 'active': True},
        )

        refresh = bool(options.get('refresh'))
        for seed in SEEDS:
            venue, created = Venue.objects.get_or_create(
                slug=seed['slug'],
                defaults={
                    'organization': organization,
                    'name': seed['name'],
                    'venue_type': seed['venue_type'],
                    'description': seed['description'],
                    'active': True,
                },
            )

            if created:
                self.stdout.write(self.style.SUCCESS(f'Created venue: {venue.name}'))
            else:
                self.stdout.write(f'Venue already exists: {venue.name}')

            current = venue.agent_configs.order_by('-version').first()
            if current and not refresh:
                self.stdout.write(f'Keeping existing configuration v{current.version} for {venue.name}')
                continue

            if current:
                venue.agent_configs.filter(active=True).update(active=False)
                next_version = current.version + 1
            else:
                next_version = 1

            config = VenueAgentConfig.objects.create(
                venue=venue,
                version=next_version,
                active=True,
                **seed['config'],
            )
            self.stdout.write(self.style.SUCCESS(f'Activated {venue.name} configuration v{config.version}'))

        self.stdout.write(self.style.SUCCESS('Flexee venue setup complete.'))
