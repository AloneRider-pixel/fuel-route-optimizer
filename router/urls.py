from django.urls import path
from . import views

app_name = 'router'

urlpatterns = [
    path('route/', views.RouteOptimizeView.as_view(), name='route-optimize'),
    path('route/<int:pk>/', views.RouteDetailView.as_view(), name='route-detail'),
    path('fuel-price/', views.FuelPriceView.as_view(), name='fuel-price'),
]
