"""
Task 3 Part 2: forms for review creation/editing and review reporting.

Neither form exposes user/movie/status/reported_by - those are always set
server-side in the view, never trusted from posted data.
"""
from django import forms

from .models import Review, ReviewReport


class ReviewForm(forms.ModelForm):
    class Meta:
        model = Review
        fields = ['rating', 'review_text']
        widgets = {
            # choices=1-5 via a select, rather than a free-typed number
            # input, so the browser itself constrains the value before the
            # model's MinValueValidator/MaxValueValidator ever runs -
            # belt-and-suspenders, not a substitute for that validation.
            'rating': forms.Select(choices=[(i, str(i)) for i in range(1, 6)]),
            'review_text': forms.Textarea(attrs={'rows': 4, 'placeholder': 'Share your thoughts about the movie...'}),
        }


class ReviewReportForm(forms.ModelForm):
    class Meta:
        model = ReviewReport
        fields = ['reason']
        widgets = {
            'reason': forms.TextInput(attrs={'placeholder': 'Why are you reporting this review?'}),
        }